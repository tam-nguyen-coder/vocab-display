// Copy to include/config.h and fill in. config.h is gitignored so the Wi-Fi password and
// the host token never reach a commit.
#pragma once

#define WIFI_SSID     "your-2.4GHz-network"
#define WIFI_PASSWORD "your-password"

// Printed by `python3 host/server.py` on startup, and shown in the web UI.
#define VOCAB_TOKEN "paste-the-token-here"

// Either a literal IP, or an mDNS name ending in .local. Prefer the .local name: it keeps
// working when DHCP hands the host a different address, which it will.
#define VOCAB_HOST "my-mac.local"
#define VOCAB_PORT 8788

// The ESP32 is 2.4 GHz only. If the network is split per band, the one the host sits on
// may well be the 5 GHz half, which the board cannot see at all -- it reports
// NO_SSID_AVAIL, indistinguishable from a typo.

// POSIX timezone string, used to work out which weekday it is locally. The board syncs
// its clock over NTP so it can tell a Saturday from a Monday on its own -- the weekend is
// exactly when the host is asleep and cannot be asked. "ICT-7" is Vietnam; note the sign
// is inverted in POSIX, so UTC+7 is written -7.
#define VOCAB_TZ "ICT-7"

// Screen orientation. 1 puts the USB port on the left; 3 flips the whole thing 180
// degrees for when the cable needs to leave the other way.
#define SCREEN_ROTATION 1

// Flipping the image does not move the buttons. Set this to 1 to also swap which one
// advances and which one marks an entry known.
#define SWAP_BUTTONS 0
