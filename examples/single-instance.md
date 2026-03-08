# Example: Migrate a Single RDS PostgreSQL Instance

This example walks through migrating a standalone RDS PostgreSQL instance from PG 13 to PG 16 using blue-green deployment.

## Scenario

- **Instance**: `prod-api-db` (db.r6g.xlarge, Multi-AZ, 200GB gp3)
- **Current version**: PostgreSQL 13.12
- **Target version**: PostgreSQL 16.3
- **Requirement**: Zero downtime

## Step 1: Pre-flight Checks

Run pre-flight checks to validate the instance is ready for migration.

```bash
rds-migrate preflight \
  --instance prod-api-db \
  --target-version 16 \
  --profile production \
  --region us-east-1
```

Expected output:

```
Pre-flight Report: prod-api-db
+---------------------------+--------+----------------------------------------------+
| Check                     | Status | Message                                      |
+---------------------------+--------+----------------------------------------------+
| PG Version Check          | PASS   | PG 13.12 -> 16 is a supported upgrade path   |
| Parameter Compatibility   | PASS   | No incompatible parameters found              |
| Extension Compatibility   | WARN   | pg_stat_statements: reset counters after      |
| Storage Space             | PASS   | 45% utilized (90GB / 200GB)                  |
| Blue-Green Support        | PASS   | Multi-AZ instance, blue-green supported       |
+---------------------------+--------+----------------------------------------------+
```

If the **Parameter Compatibility** check fails, you likely have `max_logical_replication_workers` in your custom parameter group. Remove it before proceeding.

## Step 2: Create Blue-Green Deployment

Create the deployment and wait for the green environment to be ready.

```bash
rds-migrate migrate \
  --instance prod-api-db \
  --target-version 16 \
  --wait \
  --profile production \
  --region us-east-1
```

This will:
1. Create a new parameter group for PG 16 (based on your existing custom group, minus incompatible parameters)
2. Create a blue-green deployment
3. Wait for the green environment to be available (typically 20-60 minutes)

## Step 3: Verify Green Environment

Before switchover, verify the green instance is healthy.

```bash
rds-migrate verify \
  --instance prod-api-db-green \
  --expected-version 16 \
  --profile production \
  --region us-east-1
```

## Step 4: Switchover

When ready, perform the switchover. This typically takes 30 seconds to 2 minutes.

```bash
rds-migrate migrate \
  --instance prod-api-db \
  --target-version 16 \
  --switchover \
  --profile production \
  --region us-east-1
```

## Step 5: Post-Migration Maintenance

Immediately after switchover, run maintenance to warm the cache and refresh statistics.

```bash
rds-migrate maintain \
  --instance prod-api-db \
  --analyze \
  --connection-string "host=prod-api-db.xxxxx.us-east-1.rds.amazonaws.com port=5432 dbname=myapp user=admin password=xxx" \
  --profile production \
  --region us-east-1
```

## Step 6: Final Verification

```bash
rds-migrate verify \
  --instance prod-api-db \
  --expected-version 16 \
  --check-connections \
  --profile production \
  --region us-east-1
```

## Rollback (If Needed)

If something goes wrong before switchover, you can delete the green environment:

```bash
rds-migrate rollback \
  --deployment-id bgd-xxxx \
  --profile production \
  --region us-east-1
```

Note: Rollback is only possible **before** switchover. After switchover, use point-in-time recovery to restore the previous version.

## Timeline

| Phase                 | Duration    |
|-----------------------|-------------|
| Pre-flight checks     | ~1 minute   |
| Create blue-green     | 20-60 min   |
| Verification          | ~2 minutes  |
| Switchover            | 30s - 2 min |
| Post-maintenance      | 5-30 min    |
| **Total**             | **30-90 min** |
