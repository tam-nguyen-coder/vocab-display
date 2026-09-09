"use strict";

const $ = (id) => document.getElementById(id);
const rowsEl = $("rows");
let entries = [];

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
  toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ------------------------------------------------------------------------------ render

const STAT_LABELS = [
  ["remaining", "còn lại", true],
  ["known", "đã biết", false],
  ["unseen", "chưa gặp", false],
  ["total", "tổng", false],
  ["words", "từ", false],
  ["structures", "cấu trúc", false],
];

async function loadStats() {
  const { stats, token } = await api("/api/stats");
  $("token").textContent = token;
  $("stats").innerHTML = STAT_LABELS
    .map(([key, label, accent]) =>
      `<div class="stat${accent ? " is-accent" : ""}"><b>${stats[key]}</b><span>${label}</span></div>`)
    .join("");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function rowHtml(e) {
  const nextType = e.type === "word" ? "structure" : "word";
  return `<tr data-id="${e.id}"${e.known ? ' class="known"' : ""}>
    <td class="id">${e.id}</td>
    <td><span class="badge ${e.type}" data-set-type="${nextType}" title="Đổi loại"
        >${e.type === "word" ? "từ" : "cấu trúc"}</span></td>
    <td><input class="cell front" value="${escapeHtml(e.front)}" data-field="front"></td>
    <td><input class="cell back" value="${escapeHtml(e.back)}" data-field="back"></td>
    <td><input class="cell example" value="${escapeHtml(e.example)}" data-field="example"></td>
    <td class="seen">${e.seen}</td>
    <td style="text-align:center"><input type="checkbox" data-field="known"
        ${e.known ? "checked" : ""}></td>
    <td class="actions"><button class="icon" data-delete title="Xoá">&times;</button></td>
  </tr>`;
}

async function loadEntries() {
  const params = new URLSearchParams({
    q: $("search").value,
    type: $("filter-type").value,
    status: $("filter-status").value,
  });
  const data = await api(`/api/entries?${params}`);
  entries = data.entries;
  rowsEl.innerHTML = entries.map(rowHtml).join("");
  $("empty").hidden = entries.length > 0;
}

async function refresh() {
  await Promise.all([loadEntries(), loadStats()]);
}

// ------------------------------------------------------------------------------- edits

async function save(id, patch, cell) {
  if (cell) cell.classList.add("saving");
  try {
    await api(`/api/entries/${id}`, { method: "PUT", body: JSON.stringify(patch) });
  } catch (err) {
    toast(`Không lưu được: ${err.message}`);
    await refresh();
    return;
  } finally {
    if (cell) cell.classList.remove("saving");
  }
}

// One listener on the tbody rather than per row: rows are replaced wholesale on every
// filter change, and re-attaching handlers each time is how stale listeners accumulate.
rowsEl.addEventListener("change", async (event) => {
  const row = event.target.closest("tr");
  if (!row) return;
  const id = Number(row.dataset.id);
  const field = event.target.dataset.field;
  if (!field) return;

  if (field === "known") {
    await save(id, { known: event.target.checked });
    await refresh();  // affects the counts and the row's dimming
  } else {
    await save(id, { [field]: event.target.value }, event.target);
    await loadStats();
  }
});

rowsEl.addEventListener("click", async (event) => {
  const row = event.target.closest("tr");
  if (!row) return;
  const id = Number(row.dataset.id);

  const newType = event.target.dataset.setType;
  if (newType) {
    await save(id, { type: newType });
    return refresh();
  }

  if (event.target.hasAttribute("data-delete")) {
    const entry = entries.find((e) => e.id === id);
    if (!confirm(`Xoá "${entry.front}"? Không hoàn lại được.`)) return;
    await api(`/api/entries/${id}`, { method: "DELETE" });
    toast(`Đã xoá "${entry.front}"`);
    return refresh();
  }
});

// Enter commits without waiting for blur, which is how anyone edits a table quickly.
rowsEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.classList.contains("cell")) event.target.blur();
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
  if (!body.front || !body.back) return toast("Cần cả từ và nghĩa");
  try {
    const { entry } = await api("/api/entries", { method: "POST", body: JSON.stringify(body) });
    toast(`Đã thêm "${entry.front}" (id ${entry.id})`);
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
  if (!text.trim()) return toast("Chưa có gì để nhập");
  const result = await api("/api/import", { method: "POST", body: JSON.stringify({ text }) });
  const parts = [`thêm ${result.added}`];
  if (result.skipped) parts.push(`bỏ qua ${result.skipped} (đã có)`);
  if (result.errors.length) parts.push(`${result.errors.length} dòng lỗi`);
  $("import-result").textContent = parts.join(", ") +
    (result.errors.length ? ` — ${result.errors[0]}` : "");
  if (result.added) $("import-text").value = "";
  await refresh();
});

// ------------------------------------------------------------------------------- filters

$("search").addEventListener("input", debounce(loadEntries, 180));
$("filter-type").addEventListener("change", loadEntries);
$("filter-status").addEventListener("change", loadEntries);

refresh().catch((err) => toast(`Không tải được: ${err.message}`));
