"""Market website: stdlib web server, OpenAPI reference, and the dashboard page.

Entry points::

    uv run python -m aoeo_market.web --db market.db --port 8000 --write-port 8001

``python -m aoeo_market.web`` runs :func:`aoeo_market.web.server.main`
(via :mod:`aoeo_market.web.__main__`).  The server process is the single
owner of the DuckDB snapshot database; the fetcher posts snapshots to its
``POST /api/snapshot`` endpoint, which is served on its own port
(``--write-port``, default 8001) while the dashboard and the read API stay on
``--port`` (default 8000).
"""

from __future__ import annotations

from .server import WebApp, main

__all__ = ["WebApp", "main"]
