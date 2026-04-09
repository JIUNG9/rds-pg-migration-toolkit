.PHONY: lint test typecheck all clean

lint:
	flake8 src/ tests/

test:
	pytest tests/ -v --cov=rds_migrate --cov-report=term-missing

typecheck:
	mypy src/rds_migrate/

all: lint typecheck test

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
