.DEFAULT_GOAL := help
PYTHON ?= python3
.PHONY: help run test test-cloud audit

help:
	@echo 'Independent company starter (not configured by default)'
	@echo 'Read README.md; bootstrap: python3 scripts/bootstrap_company.py --slug acme --output /path/to/new-company'
	@echo 'make run ARGS="--help"   Show the supported accounting CLI'
	@echo 'make test                Run tests and critical Python checks'
	@echo 'make test-cloud          Require the cloud extra and test real SDK dispatch'
	@echo 'make audit               Run the local starter audit'

run:
	PYTHONPATH=src $(PYTHON) -m mf_phase12 $(or $(ARGS),--help)

test:
	$(PYTHON) -m pytest -q
	$(PYTHON) -m ruff check src scripts tests

audit:
	PYTHONPATH=src $(PYTHON) -m mf_phase12.starter_audit

# Do not silently call a stub or skipped optional test a cloud integration pass.
test-cloud:
	$(PYTHON) -c "from importlib.metadata import version; print('Real MCP SDK:', version('mcp'))"
	$(PYTHON) -m pytest -q tests/test_portable_workflow_bridge.py -k real_sdk

.PHONY: test-accounting source-release
test-accounting:
	$(PYTHON) -m pytest -q tests/test_accounting_pipeline_e2e.py
	$(PYTHON) examples/accounting_pipeline_demo.py

source-release:
	$(PYTHON) scripts/build_source_release.py --output $(or $(OUTPUT),dist/source.tar.gz)
