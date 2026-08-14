# Live client — what's left

`aoeo_market/client.py` implements connect / framing / login-frame / market
query / response parse / observe loop. Two pieces need finishing against a live
login, both authentication-related:

1. **Get a session** (`xuid`, `username`, 32-char `token`). Either:
   - reproduce the launcher's account login — a **TLS WebSocket** to the Celeste
     account server taking mail+password, returning your account (incl. `Xuid`)
     and session token (see `Celeste.Launcher` → `Libs/Celeste_Public_Api/
     WebSocket_Api`); implement in `MarketClient.acquire_session`, **or**
   - extract the three values once from your own real login (sniff your 1510
     `0xF1` frame). Fine for a personal read-only tool.
2. **Complete the login handshake ordering** — the short control exchange around
   the `0xF1` frame (opcodes `0xf2/0xff/0xfe/0x91/0x92` appear in the login
   capture). Marked `TODO` in `client.py`; finish from a fresh capture.

## Operational note

The backend may permit only one live session per account. Run the game **or**
this poller, not both — ideally give the poller its own account.
