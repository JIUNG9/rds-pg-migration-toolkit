# Example: Migrate an Aurora PostgreSQL Cluster

This example demonstrates migrating an Aurora PostgreSQL cluster (1 writer + 2 readers) from PG 13 to PG 16.

## Scenario

- **Cluster**: `prod-aurora-main` (1 writer + 2 readers, db.r6g.xlarge)
- **Current version**: Aurora PostgreSQL 13.12
- **Target version**: Aurora PostgreSQL 16.3
- **Custom endpoints**: `prod-aurora-main-analytics` (reader endpoint for reporting)

## Important: Aurora-Specific Considerations

1. **Blue-green deployments include all cluster members** — writer and readers are all cloned
2. **Custom endpoints are NOT automatically migrated** — you must recreate them after switchover
3. **DNS propagation**: Cluster endpoints may take up to 30 seconds to resolve after switchover
4. **Aurora retries**: Blue-green deployments on Aurora can fail due to replication lag; use retry automation

## Step 1: Pre-flight Checks

```bash
rds-migrate preflight \
  --instance prod-aurora-main \
  --target-version 16 \
  --profile production \
  --region us-east-1
```

Pay special attention to:
- **Parameter Compatibility**: Aurora uses cluster parameter groups. Ensure `max_logical_replication_workers` is not in your custom cluster parameter group.
- **Extension Compatibility**: Aurora may have different extension versions available than standalone RDS.

## Step 2: Set Up Retry Automation (Recommended)

Aurora blue-green deployments can fail and need to be retried. Set up automatic retry before starting.

```bash
# Deploy the retry automation stack
cd templates/eventbridge-lambda/
sam deploy --guided
```

Or use the toolkit to create the EventBridge rule:

```bash
rds-migrate retry setup \
  --max-retries 3 \
  --profile production \
  --region us-east-1
```

## Step 3: Create Blue-Green Deployment

```bash
rds-migrate migrate \
  --instance prod-aurora-main \
  --target-version 16 \
  --wait \
  --timeout 7200 \
  --profile production \
  --region us-east-1
```

Aurora deployments may take longer than standalone RDS (30-90 minutes) because all cluster members are created. Use a longer timeout.

## Step 4: Verify Green Cluster

```bash
rds-migrate verify \
  --instance prod-aurora-main-green \
  --expected-version 16 \
  --check-replication \
  --profile production \
  --region us-east-1
```

Check that all reader instances are in sync:

```
Verification Report: prod-aurora-main-green
Expected Version: PG 16
Overall: PASS

  [OK] PG Version: 16.3 (matches expected)
  [OK] Connections: Cluster accepting connections
  [OK] Replication: 3 members, 0 lag
  [OK] Basic Query: SELECT 1 (3ms)
```

## Step 5: Switchover

```bash
rds-migrate migrate \
  --instance prod-aurora-main \
  --target-version 16 \
  --switchover \
  --profile production \
  --region us-east-1
```

## Step 6: Recreate Custom Endpoints

Custom endpoints are NOT preserved during switchover. Recreate them:

```bash
aws rds create-db-cluster-endpoint \
  --db-cluster-identifier prod-aurora-main \
  --db-cluster-endpoint-identifier prod-aurora-main-analytics \
  --endpoint-type READER \
  --region us-east-1 \
  --profile production
```

## Step 7: Post-Migration Maintenance

Run maintenance on the writer instance:

```bash
rds-migrate maintain \
  --instance prod-aurora-main \
  --analyze \
  --connection-string "host=prod-aurora-main.cluster-xxxxx.us-east-1.rds.amazonaws.com port=5432 dbname=myapp user=admin password=xxx" \
  --profile production \
  --region us-east-1
```

## Step 8: Clean Up Retry Automation

After successful migration, clean up the retry automation:

```bash
rds-migrate retry cleanup \
  --profile production \
  --region us-east-1
```

## Common Failure Scenarios

### Deployment Fails Due to Replication Lag

Symptom: Deployment status becomes FAILED with message about replication lag.

Solution: The retry automation will automatically retry. If it persists, check for long-running transactions on the writer instance.

### Custom Endpoints Not Resolving

Symptom: Analytics queries fail after switchover.

Solution: Custom endpoints must be manually recreated (Step 6). They are NOT preserved during switchover.

### Cold Cache on Readers

Symptom: Reader queries are slow after switchover.

Solution: Run ANALYZE and wait 10-30 minutes for the buffer cache to warm up naturally. Do NOT run VACUUM FULL.

## Timeline

| Phase                       | Duration      |
|-----------------------------|---------------|
| Pre-flight checks           | ~1 minute     |
| Retry automation setup      | ~2 minutes    |
| Create blue-green           | 30-90 min     |
| Verification                | ~5 minutes    |
| Switchover                  | 1-3 min       |
| Recreate custom endpoints   | ~2 minutes    |
| Post-maintenance            | 10-30 min     |
| **Total**                   | **45-130 min**|
