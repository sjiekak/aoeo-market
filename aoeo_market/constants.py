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

DEFAULT_POLL_INTERVAL = 30.0
"""Seconds between market polls in the live client."""

DEFAULT_CONNECT_TIMEOUT = 15.0
"""Socket connect/read timeout in seconds."""
