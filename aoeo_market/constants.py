"""Shared constants for the Project Celeste game backend.

Single source of truth for the server endpoints and client defaults so the live
client, the capture source, and the auth/login service all agree on one value.
"""

CELESTE_NETWORK_HOST = "51.91.169.108"
"""Host of the plaintext "Celeste Network" account/login service (TCP 4564)."""

CELESTE_NETWORK_PORT = 4564
"""TCP port of the account/login service (see :mod:`aoeo_market.auth`)."""

GAME_SERVER_HOST = "51.91.169.108"
"""Host of the game service."""

GAME_SERVER_PORT = 1510
"""TCP port of the game service (channels 0x0032 / 0x0101)."""

LOBBY_SERVER_PORT = 1500
"""TCP port of the lobby/realm service (channel 0x0028), observed in the
2026-08-17 capture.  The game repeats its 0xF1 login here (with version byte
0x01) to fetch realm info ("Celeste Fan Server" / "Celeste-Production1"); the
market client does not need it."""

LAUNCHER_PORT = 4513
"""TLS WebSocket port of the launcher account API (launcher-only features:
friends, news). Not used by the game and not needed by this client."""

DEFAULT_POLL_INTERVAL = 30.0
"""Seconds between market polls in the live client."""

DEFAULT_CONNECT_TIMEOUT = 15.0
"""Socket connect/read timeout in seconds."""
