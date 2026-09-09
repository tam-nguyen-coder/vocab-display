"use strict";

const $ = (id) => document.getElementById(id);
const rowsEl = $("rows");

// Every control writes here and the URL mirrors it, so a filtered view survives a reload,
// can be bookmarked, and gets browser back/forward for nothing. localStorage would have
// persisted the state too but none of the rest.
const state = {
  q: "",
  type: new Set(),
  level: new Set(),
  status: new Set(),
  daily: false,
  sort: "id",
  dir: "asc",
  per: 50,
  page: 1,
};

const MULTI = ["type", "level", "status"];
let entries = [];
let inFlight = 0;

// ---------------------------------------------------------------------------- plumbing

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 1900);
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------------------------------------------------- url <-> state

function stateToParams() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  for (const key of MULTI) {
    if (state[key].size) p.set(key, [...state[key]].join(","));
  }
  if (state.daily) p.set("daily", "1");
  if (state.sort !== "id") p.set("sort", state.sort);
  if (state.dir !== "asc") p.set("dir", state.dir);
  if (state.per !== 50) p.set("per", state.per);
  if (state.page !== 1) p.set("page", state.page);
  return p;
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = p.get("q") || "";
  for (const key of MULTI) {
    state[key] = new Set((p.get(key) || "").split(",").filter(Boolean));
  }
  state.daily = p.get("daily") === "1";
  state.sort = p.get("sort") || "id";
  state.dir = p.get("dir") === "desc" ? "desc" : "asc";
  state.per = Number(p.get("per")) || 50;
  state.page = Number(p.get("page")) || 1;
}

function writeUrl() {
  const query = stateToParams().toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
}

function syncControls() {
  $("search").value = state.q;
  $("per").value = String(state.per);
  for (const group of document.querySelectorAll(".group")) {
    const key = group.dataset.key;
    for (const pill of group.querySelectorAll(".pill")) {
      const on = key === "daily" ? state.daily : state[key].has(pill.dataset.value);
      pill.classList.toggle("on", on);
      pill.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }
  for (const th of document.querySelectorAll("th.sortable")) {
    const active = th.dataset.sort === state.sort;
    th.classList.toggle("sorted", active);
    th.dataset.dir = active ? state.dir : "";
  }
  const filtered = state.q || state.daily || MULTI.some((k) => state[k].size);
  $("clear-filters").hidden = !filtered;
}

// ------------------------------------------------------------------------------ render

const STAT_LABELS = [
  ["remaining", "left", true],
  ["known", "known", false],
  ["unseen", "never shown", false],
  ["total", "total", false],
  ["words", "words", false],
  ["structures", "structures", false],
];

function renderToday(daily, config) {
  $("today-date").textContent = daily.date || "—";
  // Only overwrite the inputs when they are not being typed into, or a background refresh
  // would fight the caret.
  for (const [id, key] of [["cfg-words", "daily_words"], ["cfg-structures", "daily_structures"]]) {
    if (document.activeElement !== $(id)) $(id).value = config[key];
  }
  $("today-chips").innerHTML = daily.entries.map((e) =>
    `<span class="chip ${e.type}"><span class="dot"></span>${escapeHtml(e.front)}` +
    `<span class="days">${e.days}d</span></span>`).join("");
}

async function loadStats() {
  const { stats, token, daily, config } = await api("/api/stats");
  $("token").textContent = token;
  $("stats").innerHTML = STAT_LABELS
    .map(([key, label, accent]) =>
      `<div class="stat${accent ? " is-accent" : ""}"><b>${stats[key]}</b><span>${label}</span></div>`)
    .join("");
  renderToday(daily, config);
}

// Accent- and case-insensitive, matching how the server searches -- highlighting only the
// literal typed form would leave "tiep can" matching a row with nothing marked on it.
function fold(text) {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

const FIELD_SCOPE = { front: ["front"], back: ["back"], ex: ["example"],
                      example: ["example"], any: ["front", "back", "example"] };

// `field` matters: searching `front:used` and then marking "used" everywhere would claim
// the example column matched when the query never looked at it.
function highlight(text, field) {
  const needles = [...(state.q.matchAll(
      /(-?)(?:(front|back|ex|example|any):)?(?:"([^"]*)"|(\S+))/gi))]
    .filter((m) => !m[1])
    .filter((m) => (FIELD_SCOPE[(m[2] || "any").toLowerCase()] || []).includes(field))
    .map((m) => m[3] ?? m[4] ?? "")
    .filter(Boolean)
    .map(fold);
  if (!needles.length) return escapeHtml(text);

  const folded = fold(text);
  const marks = new Array(text.length).fill(false);
  for (const needle of needles) {
    let at = folded.indexOf(needle);
    while (at !== -1) {
      // Folding strips combining marks, so folded and raw indices only line up while the
      // text has none. Clamp rather than risk marking past the end.
      for (let i = at; i < Math.min(at + needle.length, marks.length); i++) marks[i] = true;
      at = folded.indexOf(needle, at + needle.length);
    }
  }

  let out = "";
  for (let i = 0; i < text.length; i++) {
    const start = marks[i] && !marks[i - 1];
    const end = marks[i] && !marks[i + 1];
    out += (start ? "<mark>" : "") + escapeHtml(text[i]) + (end ? "</mark>" : "");
  }
  return out;
}

const STATUS_LABEL = { unseen: "unseen", learning: "learning", known: "known" };

function rowHtml(e) {
  const nextType = e.type === "word" ? "structure" : "word";
  return `<tr data-id="${e.id}" class="${e.known ? "known" : ""}${e.today ? " today" : ""}">
    <td class="id">${e.id}</td>
    <td>
      <span class="badge ${e.type}" data-set-type="${nextType}" title="Change type"
        >${e.type === "word" ? "word" : "structure"}</span>
      <span class="badge level" data-cycle-level title="Cycle level">${e.level}</span>
    </td>
    <td><div class="cellwrap"><input class="cell front" value="${escapeHtml(e.front)}"
        data-field="front" aria-label="term"><div class="hl front"
        >${highlight(e.front, "front")}</div></div></td>
    <td><div class="cellwrap"><input class="cell back" value="${escapeHtml(e.back)}"
        data-field="back" aria-label="meaning"><div class="hl back"
        >${highlight(e.back, "back")}</div></div></td>
    <td><div class="cellwrap"><input class="cell example" value="${escapeHtml(e.example)}"
        data-field="example" aria-label="example"><div class="hl example"
        >${highlight(e.example, "example")}</div></div></td>
    <td class="num">${e.days}</td>
    <td class="num dim">${e.seen}</td>
    <td class="num"><input type="checkbox" data-field="known" ${e.known ? "checked" : ""}
        title="${STATUS_LABEL[e.status]}"></td>
    <td class="actions"><button class="icon" data-delete title="Delete">&times;</button></td>
  </tr>`;
}

async function loadEntries() {
  const token = ++inFlight;
  $("table").classList.add("busy");
  const data = await api(`/api/entries?${stateToParams()}`);
  // A slow response for an older query must not overwrite a newer one -- typing fast in
  // the search box otherwise lands whichever request happens to finish last.
  if (token !== inFlight) return;
  $("table").classList.remove("busy");

  entries = data.entries;
  // The server clamps the page to what exists; follow it back, or deleting the last row
  // of the last page leaves an empty table with no way out.
  state.page = data.page;
  state.sort = data.sort;
  state.dir = data.dir;

  rowsEl.innerHTML = entries.map(rowHtml).join("");
  $("empty").hidden = entries.length > 0;
  $("empty").textContent = state.q || state.daily || MULTI.some((k) => state[k].size)
    ? "Nothing matches these filters."
    : "The deck is empty.";

  $("pager").hidden = data.matched === 0;
  $("pager-info").textContent =
    `${data.from}–${data.to} of ${data.matched}` +
    (data.pages > 1 ? `  ·  page ${data.page}/${data.pages}` : "");
  $("prev").disabled = data.page <= 1;
  $("next").disabled = data.page >= data.pages;

  syncControls();
  writeUrl();
}

const refresh = () => Promise.all([loadEntries(), loadStats()]);

function apply({ resetPage = true } = {}) {
  if (resetPage) state.page = 1;
  loadEntries().catch((err) => toast(err.message));
}

// ------------------------------------------------------------------------------- edits

async function save(id, patch, cell) {
  if (cell) cell.classList.add("saving");
  try {
    await api(`/api/entries/${id}`, { method: "PUT", body: JSON.stringify(patch) });
  } catch (err) {
    toast(`Could not save: ${err.message}`);
    await refresh();
  } finally {
    if (cell) cell.classList.remove("saving");
  }
}

// One listener on the tbody: rows are replaced wholesale on every filter change, and
// re-attaching handlers each time is how stale listeners accumulate.
rowsEl.addEventListener("change", async (event) => {
  const row = event.target.closest("tr");
  const field = event.target.dataset.field;
  if (!row || !field) return;
  const id = Number(row.dataset.id);

  if (field === "known") {
    await save(id, { known: event.target.checked });
    await refresh();  // moves the counts, the row's dimming, and maybe today's set
  } else {
    await save(id, { [field]: event.target.value }, event.target);
    event.target.nextElementSibling.innerHTML = highlight(event.target.value, field);
    await loadStats();
  }
});

const LEVELS = ["A2", "B1", "B2"];

rowsEl.addEventListener("click", async (event) => {
  const row = event.target.closest("tr");
  if (!row) return;
  const id = Number(row.dataset.id);
  const entry = entries.find((e) => e.id === id);

  if (event.target.dataset.setType) {
    await save(id, { type: event.target.dataset.setType });
    return apply({ resetPage: false });
  }
  if (event.target.hasAttribute("data-cycle-level")) {
    const next = LEVELS[(LEVELS.indexOf(entry.level) + 1) % LEVELS.length];
    await save(id, { level: next });
    return apply({ resetPage: false });
  }
  if (event.target.hasAttribute("data-delete")) {
    if (!confirm(`Delete "${entry.front}"? This cannot be undone.`)) return;
    await api(`/api/entries/${id}`, { method: "DELETE" });
    toast(`Deleted "${entry.front}"`);
    return refresh();
  }
});

// Swapped by a class on focus rather than by a CSS sibling selector. The selector version
// silently did nothing -- the highlight layer stayed visible over a focused input, and the
// input's own glyphs stayed transparent, because the transparency rule and the focus rule
// ended up at the same specificity with the wrong one last. A class is unambiguous.
rowsEl.addEventListener("focusin", (event) => {
  if (event.target.classList.contains("cell")) event.target.parentElement.classList.add("editing");
});
rowsEl.addEventListener("focusout", (event) => {
  if (event.target.classList.contains("cell")) event.target.parentElement.classList.remove("editing");
});

// Enter commits without waiting for blur, Escape abandons the edit -- both are what
// anyone editing a table quickly reaches for.
rowsEl.addEventListener("keydown", (event) => {
  if (!event.target.classList.contains("cell")) return;
  if (event.key === "Enter") event.target.blur();
  if (event.key === "Escape") {
    const id = Number(event.target.closest("tr").dataset.id);
    const entry = entries.find((e) => e.id === id);
    event.target.value = entry[event.target.dataset.field];
    event.target.blur();
  }
});

// ------------------------------------------------------------------ filters, sort, pages

document.querySelector(".filters").addEventListener("click", (event) => {
  const pill = event.target.closest(".pill");
  if (!pill) return;
  const key = pill.closest(".group").dataset.key;
  if (key === "daily") state.daily = !state.daily;
  else state[key].has(pill.dataset.value)
    ? state[key].delete(pill.dataset.value)
    : state[key].add(pill.dataset.value);
  syncControls();
  apply();
});

$("clear-filters").addEventListener("click", () => {
  state.q = "";
  for (const key of MULTI) state[key].clear();
  state.daily = false;
  syncControls();
  apply();
});

document.querySelector("thead").addEventListener("click", (event) => {
  const th = event.target.closest("th.sortable");
  if (!th) return;
  // Clicking the active column flips direction; a new column starts ascending, except the
  // two counters where "most" is the interesting end.
  if (state.sort === th.dataset.sort) {
    state.dir = state.dir === "asc" ? "desc" : "asc";
  } else {
    state.sort = th.dataset.sort;
    state.dir = ["days", "seen"].includes(state.sort) ? "desc" : "asc";
  }
  syncControls();
  apply();
});

$("search").addEventListener("input", debounce(() => {
  state.q = $("search").value;
  apply();
}, 200));

$("per").addEventListener("change", () => {
  state.per = Number($("per").value);
  apply();
});
$("prev").addEventListener("click", () => { state.page--; apply({ resetPage: false }); });
$("next").addEventListener("click", () => { state.page++; apply({ resetPage: false }); });
$("search-help-toggle").addEventListener("click", () => {
  $("search-help").hidden = !$("search-help").hidden;
});

// "/" to search and Escape to clear it, as long as focus is not already in a field.
document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (event.key === "/" && !typing) {
    event.preventDefault();
    $("search").focus();
  } else if (event.key === "Escape" && document.activeElement === $("search")) {
    $("search").value = "";
    state.q = "";
    apply();
  }
});

window.addEventListener("popstate", () => {
  readUrl();
  syncControls();
  loadEntries().catch((err) => toast(err.message));
});

// -------------------------------------------------------------------------- add, import

$("toggle-add").addEventListener("click", () => {
  $("add-panel").hidden = !$("add-panel").hidden;
  if (!$("add-panel").hidden) $("add-front").focus();
});
$("toggle-import").addEventListener("click", () => {
  $("import-panel").hidden = !$("import-panel").hidden;
  if (!$("import-panel").hidden) $("import-text").focus();
});

async function addEntry() {
  const body = {
    front: $("add-front").value.trim(),
    back: $("add-back").value.trim(),
    example: $("add-example").value.trim(),
    type: $("add-type").value,
    level: $("add-level").value,
  };
  if (!body.front || !body.back) return toast("A term and a meaning are both required");
  try {
    const { entry } = await api("/api/entries", { method: "POST", body: JSON.stringify(body) });
    toast(`Added "${entry.front}" (id ${entry.id})`);
    $("add-front").value = $("add-back").value = $("add-example").value = "";
    $("add-front").focus();
    await refresh();
  } catch (err) {
    toast(err.message);
  }
}

$("add-submit").addEventListener("click", addEntry);
for (const id of ["add-front", "add-back", "add-example"]) {
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") addEntry(); });
}

$("import-submit").addEventListener("click", async () => {
  const text = $("import-text").value;
  if (!text.trim()) return toast("Nothing to import");
  const result = await api("/api/import", { method: "POST", body: JSON.stringify({ text }) });
  const parts = [`${result.added} added`];
  if (result.skipped) parts.push(`${result.skipped} skipped (already there)`);
  if (result.errors.length) parts.push(`${result.errors.length} bad lines`);
  $("import-result").textContent = parts.join(", ") +
    (result.errors.length ? ` — ${result.errors[0]}` : "");
  if (result.added) $("import-text").value = "";
  await refresh();
});

// --------------------------------------------------------------------------- daily set

$("cfg-apply").addEventListener("click", async () => {
  const body = {
    daily_words: Number($("cfg-words").value),
    daily_structures: Number($("cfg-structures").value),
  };
  await api("/api/config", { method: "POST", body: JSON.stringify(body) });
  toast(`${body.daily_words} words + ${body.daily_structures} structures a day`);
  await refresh();
});

$("rebuild").addEventListener("click", async () => {
  if (!confirm("Draw a new set for today? The current one is discarded.")) return;
  await api("/api/rebuild-daily", { method: "POST" });
  toast("Drew a new set for today");
  await refresh();
});

readUrl();
syncControls();
refresh().catch((err) => toast(`Could not load: ${err.message}`));
