# ── Detect Python interpreter (prefer .venv) ────────────────────

PYTHON := $(if $(wildcard .venv/bin/python),$(CURDIR)/.venv/bin/python,python3)

# ── Help ──────────────────────────────────────────────────────────

.PHONY: help

help:
	@echo "DeepSeek Proxy — Makefile"
	@echo ""
	@echo "PyPI:"
	@echo "  make pypi-setup      Install build & twine"
	@echo "  make pypi-build      Build wheel + sdist"
	@echo "  make pypi-publish    Build + upload to PyPI"
	@echo "  make pypi-clean      Remove build-pypi/"
	@echo ""
	@echo "Version:"
	@echo "  make bump-patch      0.1.0 → 0.1.1"
	@echo "  make bump-minor      0.1.0 → 0.2.0"
	@echo "  make bump-major      0.1.0 → 1.0.0"
	@echo "  make pypi-release    bump-patch + publish"
	@echo ""
	@echo "CLI:"
	@echo "  make info            Show installation and config info"
	@echo ""
	@echo "Dev:"
	@echo "  make dev             Run from source (python ds_proxy.py)"
	@echo "  make dev-install     pip install -e ."
	@echo "  make clean           Remove all build artifacts"

# ── PyPI build/publish ──────────────────────────────────────────

PYPI_BUILD   = build-pypi
PKG_DIR      = $(PYPI_BUILD)/deepseek_proxy

.PHONY: pypi-setup pypi-build pypi-publish pypi-clean

pypi-setup:
	$(PYTHON) -m pip install build twine

pypi-build: $(PYPI_BUILD)/dist
	@echo "✓ PyPI package built at $(PYPI_BUILD)/dist/"

$(PYPI_BUILD)/dist: $(PKG_DIR)/__init__.py $(PKG_DIR)/__main__.py
	cp pyproject.toml $(PYPI_BUILD)/
	cp README_PYPI.md $(PYPI_BUILD)/README.md
	$(PYTHON) -m build $(PYPI_BUILD) --outdir $(PYPI_BUILD)/dist
	@touch $@

$(PKG_DIR):
	mkdir -p $@

$(PKG_DIR)/__init__.py: ds_proxy.py | $(PKG_DIR)
	cp $< $@

$(PKG_DIR)/__main__.py: | $(PKG_DIR)
	printf 'from deepseek_proxy import main\nmain()\n' > $@

pypi-publish: pypi-build
	cd $(PYPI_BUILD) && twine upload dist/*

pypi-clean:
	rm -rf $(PYPI_BUILD)

# ── Version bump ──────────────────────────────────────────────────

.PHONY: bump-patch bump-minor bump-major

bump-patch:
	@$(PYTHON) -c "import re; f='pyproject.toml'; c=open(f).read(); c=re.sub(r'version = \"(\d+)\.(\d+)\.(\d+)\"', lambda m: f'version = \"{m.group(1)}.{m.group(2)}.{int(m.group(3))+1}\"', c); open(f,'w').write(c); v=re.search(r'version = \"(.+?)\"',c).group(1); print(f'✓ bumped to {v}')"

bump-minor:
	@$(PYTHON) -c "import re; f='pyproject.toml'; c=open(f).read(); c=re.sub(r'version = \"(\d+)\.(\d+)\.(\d+)\"', lambda m: f'version = \"{m.group(1)}.{int(m.group(2))+1}.0\"', c); open(f,'w').write(c); v=re.search(r'version = \"(.+?)\"',c).group(1); print(f'✓ bumped to {v}')"

bump-major:
	@$(PYTHON) -c "import re; f='pyproject.toml'; c=open(f).read(); c=re.sub(r'version = \"(\d+)\.(\d+)\.(\d+)\"', lambda m: f'version = \"{int(m.group(1))+1}.0.0\"', c); open(f,'w').write(c); v=re.search(r'version = \"(.+?)\"',c).group(1); print(f'✓ bumped to {v}')"

pypi-release: bump-patch pypi-publish

# ── Development ─────────────────────────────────────────────────

.PHONY: dev dev-install

dev:
	$(PYTHON) ds_proxy.py

dev-install:
	$(PYTHON) -m pip install -e .

# ── Clean ───────────────────────────────────────────────────────

.PHONY: clean

clean: pypi-clean
	rm -rf *.egg-info dist build .eggs
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
