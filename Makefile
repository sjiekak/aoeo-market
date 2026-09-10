# Lint targets.
#
# `make lint` runs both linters: the Python backend (ruff) and the dashboard's
# JavaScript/HTML/CSS (Biome).
#
# Biome runs from a pinned Docker image, so no host Node/npm installation is
# needed and the linter version is identical locally and in CI.

.DEFAULT_GOAL := lint

# --- python -------------------------------------------------------------------

# Override to use a ruff already on PATH: make lint RUFF=ruff
RUFF ?= uv run ruff

# --- web ----------------------------------------------------------------------

BIOME_VERSION ?= 2.5.12
WEB_DIR       ?= aoeo_market/web/static

# Read-only mount: linting must never modify sources. Use `--write` explicitly
# outside of make if you want Biome to apply fixes.
BIOME_RUN ?= docker run --rm -v "$(CURDIR):/code:ro" \
	ghcr.io/biomejs/biome:$(BIOME_VERSION)

.PHONY: lint lint-python lint-web

lint: lint-python lint-web

lint-python:
	$(RUFF) check .
	$(RUFF) format --check .

lint-web:
	$(BIOME_RUN) lint $(WEB_DIR)
