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
