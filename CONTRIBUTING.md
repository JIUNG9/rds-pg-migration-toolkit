# Contributing to rds-pg-migration-toolkit

Thank you for your interest in contributing. This toolkit was born from real-world experience migrating 49 RDS/Aurora PostgreSQL instances, and contributions from the community make it better for everyone.

## Getting Started

### Prerequisites

- Python 3.9+
- AWS CLI configured (for integration tests)
- Git

### Development Setup

```bash
# Clone the repository
git clone https://github.com/junegu/rds-pg-migration-toolkit.git
cd rds-pg-migration-toolkit

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install in development mode with dev dependencies
pip install -e ".[dev]"

# Verify installation
rds-migrate --help
```

### Running Tests

```bash
# Run all unit tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=rds_migrate --cov-report=term-missing

# Run only unit tests (no AWS required)
pytest tests/ -v -m "not integration"

# Run integration tests (requires AWS credentials)
pytest tests/ -v -m integration
```

### Linting and Type Checking

```bash
# Lint
flake8 src/ tests/ --max-line-length 120

# Type check
mypy src/rds_migrate/ --ignore-missing-imports
```

## How to Contribute

### Reporting Bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include:

- Full CLI output (use `--verbose`)
- Python version, toolkit version
- RDS/Aurora engine and version
- AWS region

### Suggesting Features

Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md). Describe:

- The problem you are solving
- Your proposed CLI interface
- Real-world use case

### Submitting Pull Requests

1. **Fork** the repository and create a branch from `main`.
2. **Name your branch** descriptively: `fix/parameter-check-pg16`, `feat/batch-scheduling`, `docs/aurora-gotchas`.
3. **Write tests** for any new functionality.
4. **Update documentation** if your change affects CLI behavior or adds new features.
5. **Run the full test suite** before submitting.
6. **Open a pull request** with a clear description of what and why.

### Code Style

- Follow PEP 8 with a max line length of 120 characters.
- Use type hints for all function signatures.
- Write docstrings for all public functions and classes.
- Use `click` for CLI commands and `rich` for output formatting.
- Prefer explicit over implicit: name variables clearly, avoid abbreviations.

### Commit Messages

Use conventional commits:

```
feat: add batch migration scheduling
fix: handle removed parameters in PG 16 preflight check
docs: add Aurora cluster migration example
test: add unit tests for parameter compatibility check
chore: update boto3 dependency
```

## Areas Where Help Is Needed

- **Additional PG version support**: Testing with PG 14 and PG 15 as source versions.
- **Aurora Serverless v2**: Blue-green deployment behavior with Serverless v2.
- **Multi-region**: Cross-region migration support.
- **Monitoring integrations**: CloudWatch alarms, Datadog, PagerDuty notifications during migration.
- **Dry-run mode**: Simulate migration without making AWS API calls.
- **Cost estimation**: Estimate the cost of running blue-green environments.

## Code of Conduct

Be respectful and constructive. We are all here to build better tools for database migrations. Disagreements on implementation are welcome; personal attacks are not.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
