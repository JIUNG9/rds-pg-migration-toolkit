# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Documentation
- README: document the optional `[db]` extra and that only `maintain` needs `psycopg2` and a direct database connection.

### Fixed
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
