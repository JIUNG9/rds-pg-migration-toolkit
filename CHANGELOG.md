# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Removed
- The `Parameter Compatibility` pre-flight check, `REMOVED_PARAMETERS`, and the filter in `migrate.py` that dropped `max_logical_replication_workers` from the green parameter group. They were built on the claim that PostgreSQL 16 removed that parameter. It exists in PostgreSQL 16 ([docs](https://www.postgresql.org/docs/16/runtime-config-replication.html)), so the filter silently dropped a valid setting. The green parameter group now receives every user-modified parameter from the source group. `preflight` runs 4 checks instead of 5. The 0.5.0 entry below describes the removed check as shipped.

### Documentation
- README and CONTRIBUTING said Python 3.9+ and `pyproject.toml` listed a 3.9 classifier, but `requires-python` is `>=3.10` and CI tests 3.10 to 3.13. All three now say 3.10+, and the classifiers list 3.10 to 3.13.
- README: document the optional `[db]` extra and that only `maintain` needs `psycopg2` and a direct database connection.

### Fixed
- `maintain` exited 0 when a task failed (connection refused, SQL error), because only skipped tasks were counted. It now prints the report and exits 1 for a failed task as well. The README lists the exit codes.
- `maintain` exited 0 when a requested task was skipped because `psycopg2` was missing or `--connection-string` was not given, so a script could not tell that nothing ran. It now prints the report and exits 1. A task skipped for a benign reason (no index above the size threshold) still exits 0.
- `rollback_deployment` returned `DELETE_IN_PROGRESS` when the green environment was kept, although nothing is deleted. It now returns the new `CANCELLED` status, so `rollback` without `--delete-green` prints `Status: CANCELLED`.
- `rollback --delete-green` could not be turned off (`default=True` on a flag), so every rollback deleted the green environment. It now defaults to keeping it; `--delete-green` asks for confirmation, `--yes` skips it.

## [0.5.0] - 2026-04-10

### Added
- Rich table, JSON, and CSV output formatting for all report types
- `Makefile` with `lint`, `test`, `typecheck` targets
- Blog cross-reference in README

### Changed
- Updated pyproject.toml version to 0.5.0

## [0.4.0] - 2026-04-03

### Added
- EventBridge + Lambda retry automation for failed blue-green deployments
- SAM/CloudFormation template generation for serverless deployment
- Retry rule listing and cleanup with correct dependency ordering
- Exponential backoff with SSM-tracked retry counts

## [0.3.0] - 2026-03-28

### Added
- Post-migration verification: version check, connection health, replication lag, query latency
- Maintenance operations: `ANALYZE VERBOSE`, `REINDEX CONCURRENTLY`, bloat detection
- Optional `psycopg2` dependency for direct database operations (`pip install rds-pg-migration-toolkit[db]`)

## [0.2.0] - 2026-03-15

### Added
- Pre-flight version check with upgrade path validation (PG 12-16)
- Parameter compatibility detection (catches `max_logical_replication_workers` removal in PG 16)
- Extension compatibility warnings (`pg_stat_statements`, `pglogical`, `postgis`)
- Storage space check via CloudWatch `FreeStorageSpace` metric
- Blue-green deployment eligibility validation
- `resolve_engine_version()` for latest minor version resolution
- `get_instance_arn()` with Aurora cluster fallback

## [0.1.0] - 2026-03-08

### Added
- Initial scaffold: CLI with `preflight`, `migrate`, `verify`, `maintain`, `rollback` commands
- Click-based CLI with global options (`--profile`, `--region`, `--output`, `--verbose`)
- Data models: `CheckResult`, `PreflightReport`, `DeploymentResult`, `VerifyCheck`, `MaintenanceTask`
- Rich table rendering for pre-flight reports
- YAML configuration loading with defaults
- Comprehensive documentation: README, gotchas, examples, CONTRIBUTING
