# ── PyPI build/publish ──────────────────────────────────────────

PYPI_BUILD   = build-pypi
PKG_DIR      = $(PYPI_BUILD)/deepseek_proxy

.PHONY: pypi-build pypi-publish pypi-clean

pypi-build: $(PYPI_BUILD)/dist
	@echo "✓ PyPI package built at $(PYPI_BUILD)/dist/"

$(PYPI_BUILD)/dist: $(PKG_DIR)/__init__.py $(PKG_DIR)/__main__.py
	cp pyproject.toml README.md $(PYPI_BUILD)/
	cd $(PYPI_BUILD) && python -m build --outdir dist
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

# ── Development ─────────────────────────────────────────────────

.PHONY: dev dev-install

dev:
	python ds_proxy.py

dev-install:
	pip install -e .

# ── Clean ───────────────────────────────────────────────────────

.PHONY: clean

clean: pypi-clean
	rm -rf *.egg-info dist build .eggs
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
