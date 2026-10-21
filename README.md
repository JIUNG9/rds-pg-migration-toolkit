# rds-pg-migration-toolkit

[![CI](https://github.com/JIUNG9/rds-pg-migration-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/JIUNG9/rds-pg-migration-toolkit/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A CLI toolkit for safe, zero-downtime PostgreSQL major version upgrades on AWS RDS/Aurora using blue-green deployments.

Built from real-world experience migrating **49 RDS/Aurora PostgreSQL instances from PG 13 to PG 16** in a multi-account AWS environment.

---

## Problem

PostgreSQL major version upgrades on AWS RDS/Aurora are risky:

- **Downtime risk**: In-place upgrades require maintenance windows and can fail mid-way.
- **Parameter incompatibilities**: Parameters like `max_logical_replication_workers` were removed in PG 16, causing deployment failures if not caught early.
- **Cold cache**: After switchover, the buffer cache is empty, leading to degraded performance for minutes to hours.
- **No built-in automation**: AWS provides blue-green deployments but no orchestration for pre-flight checks, batch migrations, or post-migration maintenance.
- **Aurora quirks**: Aurora clusters have different blue-green deployment behavior than standalone RDS instances, requiring separate handling and retry logic.

This toolkit automates the entire lifecycle: **pre-flight validation, blue-green deployment, verification, and post-migration maintenance**.

---

## Features

- **Pre-flight checks** — Validate parameter group compatibility, extension support, storage space, and blue-green deployment eligibility before touching anything.
- **Blue-green deployment automation** — Create and monitor blue-green deployments via boto3 with progress tracking.
- **Parameter compatibility validation** — Detect removed/changed parameters (e.g., `max_logical_replication_workers` in PG 16) before they cause failures.
- **Post-migration maintenance** — Automated `ANALYZE`, `REINDEX`, and bloat detection to restore performance after switchover.
- **Aurora retry automation** — EventBridge + Lambda templates for automatic retry of failed blue-green deployments.
- **Batch migration support** — Migrate multiple instances with configurable scheduling and parallelism.
- **Rich CLI output** — Color-coded pass/fail reports, progress bars, and formatted tables.

---

## Architecture

```
                         rds-pg-migration-toolkit
                         ========================

  +------------------+     +------------------+     +------------------+
  |   PRE-FLIGHT     |     |    MIGRATE       |     |    VERIFY        |
  |                  |     |                  |     |                  |
  | - PG version     | --> | - Create B/G     | --> | - PG version     |
  | - Param compat   |     |   deployment     |     | - Connections    |
  | - Extensions     |     | - Monitor status |     | - Replication    |
  | - Storage space  |     | - Switchover     |     | - Query perf     |
  | - B/G support    |     | - Handle Aurora  |     |                  |
  +------------------+     +------------------+     +------------------+
                                                            |
                                                            v
  +------------------+     +------------------+     +------------------+
  |   ROLLBACK       |     |  RETRY (Aurora)  |     |    MAINTAIN      |
  |                  |     |                  |     |                  |
  | - Delete green   |     | - EventBridge    |     | - ANALYZE all    |
  | - Restore param  |     |   rule creation  |     | - REINDEX crit.  |
  |   groups         |     | - Lambda retry   |     | - Bloat check    |
  | - DNS failback   |     | - Backoff logic  |     | - Cache warmup   |
  +------------------+     +------------------+     +------------------+


  Blue-Green Deployment Flow:
  ===========================

  +-----------+          +-----------+
  |  BLUE     |          |  GREEN    |
  |  (PG 13)  |  ------> |  (PG 16)  |
  |  Current  |  Repl.  |  Staging  |
  +-----------+          +-----------+
       |                      |
       |    SWITCHOVER        |
       |  <----- DNS ------>  |
       |                      |
  +-----------+          +-----------+
  |  OLD BLUE |          |  NEW PROD |
  |  (PG 13)  |          |  (PG 16)  |
  |  Standby  |          |  Active   |
  +-----------+          +-----------+
```

---

## Quick Start

### Install

```bash
pip install rds-pg-migration-toolkit
```

`preflight`, `migrate`, `verify` and `rollback` only call the AWS APIs. `maintain` also opens a direct
PostgreSQL connection and needs the optional `[db]` extra:

```bash
pip install "rds-pg-migration-toolkit[db]"
```

### Run pre-flight checks

```bash
rds-migrate preflight --instance mydb --target-version 16
```

### Execute migration

```bash
rds-migrate migrate --instance mydb --target-version 16 --wait
```

### Verify after switchover

```bash
rds-migrate verify --instance mydb --expected-version 16
```

### Post-migration maintenance

```bash
rds-migrate maintain --instance mydb --analyze --reindex \
  --connection-string "host=mydb.example.internal port=5432 dbname=app user=maint password=..."
```

This is the only command that needs `psycopg2` (the `[db]` extra above) and network access to the database
itself, not just AWS credentials. Without the extra, or without `--connection-string`, each requested task is
reported as `skipped` with the reason and nothing is changed.

---

## CLI Commands

| Command     | Description                                              | Example                                                        |
|-------------|----------------------------------------------------------|----------------------------------------------------------------|
| `preflight` | Run pre-flight checks before migration                   | `rds-migrate preflight --instance mydb --target-version 16`    |
| `migrate`   | Create blue-green deployment and optionally switchover    | `rds-migrate migrate --instance mydb --target-version 16`      |
| `verify`    | Verify migration success (version, connections, queries)  | `rds-migrate verify --instance mydb --expected-version 16`     |
| `maintain`  | Run post-migration maintenance (ANALYZE, REINDEX, bloat)  | `rds-migrate maintain --instance mydb --analyze --reindex`     |
| `rollback`  | Roll back a failed or unwanted migration                  | `rds-migrate rollback --deployment-id bgd-xxxx`                |

`rollback` keeps the green environment unless you pass `--delete-green`, which asks for confirmation first (`--yes` skips the prompt).

### Global Options

| Option          | Description                           | Default         |
|-----------------|---------------------------------------|-----------------|
| `--profile`     | AWS CLI profile                       | `default`       |
| `--region`      | AWS region                            | `us-east-1`     |
| `--output`      | Output format (`table`, `json`, `csv`)| `table`         |
| `--verbose`     | Enable debug logging                  | `false`         |

---

## Known Gotchas

These are real issues encountered during the migration of 49 instances:

### 1. `max_logical_replication_workers` removed in PG 16

This parameter existed in PG 13 but was removed in PG 16. If your custom parameter group includes it, the blue-green deployment will fail silently or with a cryptic error. The `preflight` command detects this automatically.

### 2. SSL enforcement changes

PG 16 enforces stricter SSL defaults. Connections using `sslmode=prefer` may behave differently. Verify your application connection strings.

### 3. Cold cache after switchover

After blue-green switchover, the green instance has an empty buffer cache. Expect 2-10x slower queries for the first 5-30 minutes depending on dataset size. Use `rds-migrate maintain --analyze` immediately after switchover.

### 4. VACUUM FULL considerations

Do NOT run `VACUUM FULL` immediately after migration on large tables. It acquires an `ACCESS EXCLUSIVE` lock. Use regular `VACUUM` and `ANALYZE` instead — the `maintain` command handles this correctly.

### 5. Aurora cluster endpoint DNS propagation

Aurora cluster endpoints may take up to 30 seconds to resolve to the new writer after switchover. Build retry logic into your application or use the `retry` module.

See [docs/gotchas.md](docs/gotchas.md) for the full list with detailed explanations.

---

## Requirements

- Python 3.9+
- AWS CLI configured with appropriate permissions
- boto3
- Permissions: `rds:*`, `ec2:Describe*`, `events:Put*`, `lambda:*` (for retry automation)

---

## Project Structure

```
rds-pg-migration-toolkit/
  src/rds_migrate/
    __init__.py         # Version info
    cli.py              # Click CLI entry point
    preflight.py        # Pre-flight checks
    migrate.py          # Blue-green deployment automation
    verify.py           # Post-migration verification
    maintain.py         # Post-migration maintenance
    retry.py            # Aurora retry automation
    utils.py            # Shared utilities
  tests/
    conftest.py         # Pytest fixtures
    test_preflight.py   # Pre-flight check tests
    test_migrate.py     # Migration tests
  examples/
    single-instance.md  # Single RDS instance migration
    aurora-cluster.md   # Aurora cluster migration
    batch-migration.md  # Batch migration with scheduling
  templates/
    eventbridge-lambda/
      template.yaml     # SAM template for retry automation
  docs/
    gotchas.md          # Detailed PG 16 gotchas
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

MIT License. See [LICENSE](LICENSE).

---

## Author

**June Gu** — Site Reliability Engineer

- Experience: 49 RDS/Aurora PostgreSQL migrations (PG 13 to PG 16) across a multi-account AWS environment
- Background: AWS multi-account hub-spoke architecture, Terraform, EKS, ArgoCD
- [GitHub](https://github.com/JIUNG9)

---

## Related Articles

- [Zero-Downtime PostgreSQL 13 to 16: Lessons from Migrating 49 RDS Instances](https://medium.com/@June-Gu) — The real-world experience behind this toolkit, including the `max_logical_replication_workers` discovery and cold cache mitigation
