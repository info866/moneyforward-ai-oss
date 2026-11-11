.DEFAULT_GOAL := help
PYTHON ?= python3
.PHONY: help run test audit

help:
	@echo 'Independent company starter (not configured by default)'
	@echo 'Read README.md; bootstrap: python3 scripts/bootstrap_company.py --slug acme --output /path/to/new-company'
	@echo 'make run ARGS="--help"   Show the supported accounting CLI'
	@echo 'make test                Run tests and critical Python checks'
	@echo 'make audit               Run the local starter audit'

run:
	PYTHONPATH=src $(PYTHON) -m mf_phase12 $(or $(ARGS),--help)

test:
	$(PYTHON) -m pytest -q
	$(PYTHON) -m ruff check src scripts tests

audit:
	PYTHONPATH=src $(PYTHON) -m mf_phase12.starter_audit
