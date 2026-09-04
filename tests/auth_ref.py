"""Known xlive.dll CRC-32 build values, captured from real login traffic.

These are test-only reference data: the production client always derives the
install-signature CRC-32 from the live Celeste manifest
(``aoeo_market.auth.fetch_xlive_crc32``) and never uses these constants.

Provenance (see docs/authentication.md):

* ``XLIVE_CRC32``               — ``8ca16109``, LE bytes of ``0x0961A18C``,
  the CRC-32 of xlive.dll 1.0.0.106 (shipped by the 2026-09-03 maintenance;
  the official client's 2026-09-04 login).
* ``XLIVE_CRC32_PRE_UPGRADE``  — ``f69b991a``, LE bytes of ``0x1A999BF6``,
  the CRC-32 of the pre-upgrade xlive.dll build (rejected since 2026-09-03).
* ``XLIVE_CRC32_ALT``          — ``458e0d1e``, LE bytes of ``0x1E0D8E45``,
  the CRC-32 of the xlive.dll build machine A had on 2026-08-10.
"""

from __future__ import annotations

XLIVE_CRC32 = bytes.fromhex("8ca16109")
XLIVE_CRC32_PRE_UPGRADE = bytes.fromhex("f69b991a")
XLIVE_CRC32_ALT = bytes.fromhex("458e0d1e")
