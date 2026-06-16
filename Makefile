.PHONY: test

PYTHON ?= python3

test:
	$(PYTHON) -m pytest --cov=backend --cov-report=term
