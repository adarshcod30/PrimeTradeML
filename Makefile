PYTHON ?= python3
VENV ?= .venv
ACTIVATE = . $(VENV)/bin/activate

.PHONY: install run test docker-build docker-run clean

install:
	$(PYTHON) -m venv $(VENV)
	$(ACTIVATE) && python -m pip install -r requirements.txt pytest

run:
	$(ACTIVATE) && python run.py --input data.csv --config config.yaml --output metrics.json --log-file run.log

test:
	$(ACTIVATE) && pytest tests/test_pipeline.py -q

docker-build:
	docker build -t mlops-task .

docker-run:
	docker run --rm mlops-task

clean:
	rm -rf __pycache__ .pytest_cache tests/__pycache__
