"""``python -m aoeo_market.web`` entry point (see :mod:`aoeo_market.web`)."""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
