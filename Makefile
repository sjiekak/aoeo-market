# Lint and format targets.
#
# `make lint` runs both linters: the Python backend (ruff) and the dashboard's
# JavaScript/HTML/CSS (Biome). `make format` applies the matching formatters.
#
# Biome runs from a pinned Docker image, so no host Node/npm installation is
# needed and the tool version is identical locally and in CI.

.DEFAULT_GOAL := lint

# --- python -------------------------------------------------------------------

# Override to use a ruff already on PATH: make lint RUFF=ruff
RUFF ?= uv run ruff

# --- web ----------------------------------------------------------------------

BIOME_VERSION ?= 2.5.12
WEB_DIR       ?= aoeo_market/web/static
BIOME_IMAGE   ?= ghcr.io/biomejs/biome:$(BIOME_VERSION)

# The lint run mounts the sources read-only: linting must never modify files.
BIOME_LINT  ?= docker run --rm -v "$(CURDIR):/code:ro" $(BIOME_IMAGE)
BIOME_WRITE ?= docker run --rm -v "$(CURDIR):/code" $(BIOME_IMAGE)

# biome.json disables the formatter so that CI's `biome ci` stays a lint-only
# gate and never rewrites the deliberately compact style.css. `make format-web`
# opts back in, per language.
BIOME_FORMAT_FLAGS ?= --javascript-formatter-enabled=true \
	--css-formatter-enabled=true \
	--html-formatter-enabled=true

.PHONY: lint lint-python lint-web format format-python format-web

lint: lint-python lint-web

lint-python:
	$(RUFF) check .
	$(RUFF) format --check .

lint-web:
	$(BIOME_LINT) lint $(WEB_DIR)

format: format-python format-web

format-python:
	$(RUFF) format .

format-web:
	$(BIOME_WRITE) format --write $(BIOME_FORMAT_FLAGS) $(WEB_DIR)
