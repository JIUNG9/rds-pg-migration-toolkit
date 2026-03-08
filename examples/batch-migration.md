# Example: Batch Migration of Multiple Instances

This example demonstrates migrating multiple RDS/Aurora PostgreSQL instances in a coordinated batch, based on real experience migrating 49 instances across a multi-account AWS environment.

## Scenario

- **Instances**: 49 RDS/Aurora PostgreSQL instances across 5 AWS accounts
- **Current version**: Mix of PG 13.9, 13.11, and 13.12
- **Target version**: PostgreSQL 16.3
- **Constraint**: Maximum 5 concurrent blue-green deployments per account (AWS limit)
- **Strategy**: Migrate in waves — non-production first, production last

## Migration Waves

| Wave | Environment | Instances | Schedule         |
|------|-------------|-----------|------------------|
| 1    | Dev         | 8         | Week 1, Mon-Tue  |
| 2    | Staging     | 12        | Week 1, Wed-Fri  |
| 3    | Prod (low)  | 15        | Week 2, Mon-Wed  |
| 4    | Prod (high) | 14        | Week 2, Thu-Fri  |

## Step 1: Create an Inventory File

Create a YAML file listing all instances to migrate.

```yaml
# migration-inventory.yml
waves:
  - name: dev
    account_profile: dev-account
    region: us-east-1
    instances:
      - id: dev-api-db
        type: rds
        priority: 1
      - id: dev-aurora-main
        type: aurora
        priority: 2
      - id: dev-auth-db
        type: rds
        priority: 3
      # ... more instances

  - name: staging
    account_profile: staging-account
    region: us-east-1
    instances:
      - id: staging-api-db
        type: rds
        priority: 1
      # ... more instances

  - name: production-low
    account_profile: prod-account
    region: us-east-1
    instances:
      - id: prod-internal-tools-db
        type: rds
        priority: 1
      # ... more instances

  - name: production-high
    account_profile: prod-account
    region: us-east-1
    instances:
      - id: prod-api-db
        type: aurora
        priority: 1
      # ... more instances

target_version: "16"
max_concurrent: 5
```

## Step 2: Run Pre-flight Checks on All Instances

Run pre-flight checks across all instances before starting any migrations.

```bash
# Check all instances in a wave
for instance in dev-api-db dev-aurora-main dev-auth-db; do
  echo "=== Checking $instance ==="
  rds-migrate preflight \
    --instance "$instance" \
    --target-version 16 \
    --profile dev-account \
    --region us-east-1
  echo ""
done
```

Alternatively, use a script to process the inventory file:

```bash
#!/bin/bash
# preflight-all.sh — Run pre-flight checks on all instances in a wave

WAVE=$1
PROFILE=$2

# Parse instances from inventory (using yq or similar)
instances=$(yq ".waves[] | select(.name == \"$WAVE\") | .instances[].id" migration-inventory.yml)

failed=0
for instance in $instances; do
  echo "=== Pre-flight: $instance ==="
  rds-migrate preflight \
    --instance "$instance" \
    --target-version 16 \
    --profile "$PROFILE" \
    --region us-east-1 \
    --output json | tee "preflight-$instance.json"

  # Check if any checks failed
  if grep -q '"status": "FAIL"' "preflight-$instance.json"; then
    echo "FAILED: $instance has failing pre-flight checks"
    failed=$((failed + 1))
  fi
done

echo ""
echo "=== Summary: $failed failures out of $(echo $instances | wc -w) instances ==="
```

## Step 3: Migrate in Waves

### Wave 1: Dev Environment

```bash
# Start up to 5 concurrent deployments
rds-migrate migrate --instance dev-api-db --target-version 16 --wait --profile dev-account --region us-east-1 &
rds-migrate migrate --instance dev-aurora-main --target-version 16 --wait --profile dev-account --region us-east-1 &
rds-migrate migrate --instance dev-auth-db --target-version 16 --wait --profile dev-account --region us-east-1 &
# ... up to 5 concurrent

wait  # Wait for all background jobs

# Switchover all dev instances
for instance in dev-api-db dev-aurora-main dev-auth-db; do
  rds-migrate migrate \
    --instance "$instance" \
    --target-version 16 \
    --switchover \
    --profile dev-account \
    --region us-east-1
done
```

### Wave 2-4: Repeat for Each Environment

Apply the same pattern. For production waves, add extra verification before switchover:

```bash
# Extra verification for production
rds-migrate verify \
  --instance prod-api-db-green \
  --expected-version 16 \
  --check-connections \
  --check-replication \
  --profile prod-account \
  --region us-east-1
```

## Step 4: Post-Migration Maintenance (All Instances)

```bash
#!/bin/bash
# maintain-all.sh — Run post-migration maintenance on all instances

instances="dev-api-db dev-aurora-main staging-api-db prod-api-db"  # etc.

for instance in $instances; do
  echo "=== Maintaining $instance ==="
  rds-migrate maintain \
    --instance "$instance" \
    --analyze \
    --check-bloat \
    --profile prod-account \
    --region us-east-1
done
```

## Step 5: Final Verification

```bash
#!/bin/bash
# verify-all.sh — Verify all instances are on PG 16

instances="dev-api-db dev-aurora-main staging-api-db prod-api-db"  # etc.

for instance in $instances; do
  rds-migrate verify \
    --instance "$instance" \
    --expected-version 16 \
    --profile prod-account \
    --region us-east-1
done
```

## Lessons Learned from 49-Instance Migration

1. **Always pre-flight all instances before starting any migrations.** One instance with `max_logical_replication_workers` can block the entire wave.

2. **Respect the 5 concurrent deployment limit per account.** Exceeding it causes API throttling.

3. **Migrate dev and staging first.** This catches parameter group issues and extension incompatibilities before they hit production.

4. **Aurora clusters take longer.** Plan for 60-90 minutes for Aurora vs 20-40 minutes for standalone RDS.

5. **Schedule production migrations during low-traffic windows.** The cold cache after switchover impacts query latency for 5-30 minutes.

6. **Keep the old blue environment for 24-48 hours.** Delete only after confirming production is stable.

7. **Monitor CloudWatch metrics closely after switchover.** Key metrics: DatabaseConnections, ReadLatency, WriteLatency, FreeableMemory.
