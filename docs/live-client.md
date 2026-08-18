# Live client — status

`aoeo_market/client.py` implements connect / framing / login bundle / market
query / response parse / observe loop. The login path was fully reversed from
two independent captures (2026-08-10 and 2026-08-17) and **validated live**
from the capture machine (2026-08-17):

```
$ uv run python -m aoeo_market.live_probe --local-ip <your-ip> --game
OK - 4564 login accepted
  xuid        = <64-bit account id>
  username    = <profile name>
  token       = <32-char session token>   # same token as the Aug-13 capture
  external_ip = <public IP as seen by the server>
  sent 0xF1 login bundle (8 messages, counters 1..8)
  server reply: … bytes (0xF2 received: True)
```

Followed by a live market sweep: the default captured query shapes returned a
657-listing snapshot and the observer emitted 657 `LISTED` events, with a
second poll correctly reporting 0 changes.

What the live run confirmed:

- `MarketClient.acquire_session(mail, password, local_ip)` — the plaintext
  TCP 4564 "Celeste Network" login works with the machine-specific constants
  in `aoeo_market/auth.py` (`LOGIN_TAIL_OPAQUE` and `DEVICE_HASH`, captured
  per install); the server re-issued the exact token seen in the 2026-08-13
  capture.
- `MarketClient.login(session)` — the TCP 1510 login: 0xF1 frame + the
  eight-message bundle (counters 1..8), byte-identical to what the game
  sends; the server answered 0xF2 with the `02 01` status prefix and the
  initial `<Empire><Offers>` document.
- `MarketClient.poll_once()` — the captured ten-query selector sweep returns
  the whole market (an all-wildcard query is *not* answered), and the
  UTF-8/UTF-16 XML parsing + observer diffing behave as designed.

Remaining caveats:

- The 4564 tail/hash constants are **per install**. Running from another
  machine requires re-capturing a login there and refreshing
  `LOGIN_TAIL_OPAQUE` / `DEVICE_HASH`.
- The game re-logs in (4564 packet 7) shortly before the 1510 login; the
  client currently only does packets 1+2, which the server accepted fine.

## Operational note

The backend may permit only one live session per account. Run the game **or**
this poller, not both — ideally give the poller its own account.
