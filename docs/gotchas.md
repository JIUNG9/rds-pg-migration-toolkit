# PostgreSQL 16 Migration Gotchas

Detailed documentation of issues encountered during the migration of 49 RDS/Aurora PostgreSQL instances from PG 13 to PG 16. These are real-world findings, not theoretical concerns.

---

## 1. `max_logical_replication_workers` Removed in PG 16

**Severity**: Critical (deployment blocker)

**Problem**: The parameter `max_logical_replication_workers` existed in PostgreSQL 13-15 but was removed in PostgreSQL 16. If your custom RDS parameter group includes this parameter, the blue-green deployment will fail.

**Symptoms**:
- Blue-green deployment creation fails with a vague error
- AWS Console shows: "Invalid parameter group" or "Parameter not found"
- The error message does NOT clearly state which parameter is the issue

**Detection**:
```bash
rds-migrate preflight --instance mydb --target-version 16
```

The preflight check cross-references your custom parameter group against the known list of removed parameters.

**Fix**:
1. Create a new parameter group based on the `postgres16` family
2. Copy all parameters from your PG 13 group EXCEPT `max_logical_replication_workers`
3. Use the new parameter group for the blue-green deployment

```sql
-- Verify the parameter is set in your current group
-- Connect to the database and run:
SHOW max_logical_replication_workers;
```

**Background**: In PG 16, logical replication worker management was consolidated. The `max_logical_replication_workers` was replaced by improved internal logic in `max_worker_processes` and `max_logical_replication_workers` was removed entirely.

---

## 2. SSL Enforcement Changes

**Severity**: High (connection failures)

**Problem**: PostgreSQL 16 changes default SSL behavior. The `rds.force_ssl` parameter behavior and default SSL cipher suites differ between PG 13 and PG 16.

**Symptoms**:
- Application connections fail after switchover with SSL handshake errors
- Clients using `sslmode=prefer` may silently downgrade to non-SSL on PG 13 but fail on PG 16
- Legacy applications with old SSL libraries may not support PG 16's cipher requirements

**Detection**:
Check your application connection strings:
```bash
# Look for sslmode in connection strings
grep -r "sslmode" /path/to/your/application/config/
```

**Fix**:
1. Explicitly set `sslmode=require` or `sslmode=verify-full` in all connection strings
2. Ensure `rds.force_ssl = 1` in your parameter group (recommended)
3. Update client SSL libraries if using very old versions
4. Test connections from your green environment before switchover

**Recommendation**: Set `rds.force_ssl = 1` in both old and new parameter groups before migration. This ensures consistent behavior and exposes any SSL issues before the migration window.

---

## 3. Cold Cache After Switchover

**Severity**: High (performance degradation)

**Problem**: After blue-green switchover, the new primary (formerly green) has an empty shared buffer cache. All queries hit disk until the cache warms up.

**Impact**:
- Query latency increases 2-10x for the first 5-30 minutes
- High I/O utilization on the new primary
- CloudWatch metrics show spikes in ReadLatency and ReadIOPS
- Duration depends on dataset size and query patterns

**Symptoms**:
- Slow application response times immediately after switchover
- CloudWatch FreeableMemory drops as cache fills
- Buffer cache hit ratio drops to near 0% then gradually recovers

**Mitigation**:
1. **Run ANALYZE immediately after switchover** to refresh optimizer statistics:
   ```bash
   rds-migrate maintain --instance mydb --analyze
   ```

2. **Schedule migration during low-traffic periods** to reduce the impact of cold cache

3. **Pre-warm by running common queries** if you have a known workload:
   ```sql
   -- Example: pre-warm a critical table
   SELECT pg_prewarm('critical_table');
   ```
   Note: `pg_prewarm` extension must be installed

4. **DO NOT run VACUUM FULL** as a response to slow queries. It makes things worse (see gotcha #4)

5. **Alert your on-call team** about expected latency increase during the migration window

---

## 4. VACUUM FULL Considerations

**Severity**: High (can cause downtime)

**Problem**: Running `VACUUM FULL` immediately after migration is tempting but dangerous. It acquires `ACCESS EXCLUSIVE` locks on the table, blocking ALL reads and writes.

**Why it is tempting**: After migration, tables may show bloat from the replication process. The instinct is to clean up immediately.

**Why it is dangerous**:
- `ACCESS EXCLUSIVE` lock blocks all concurrent transactions
- On large tables, VACUUM FULL can take hours
- During this time, the table is completely unavailable
- This effectively causes downtime — exactly what blue-green was supposed to avoid

**Correct approach**:
1. Run regular `VACUUM` (no FULL) — this does NOT block reads:
   ```sql
   VACUUM ANALYZE my_table;
   ```

2. Run `ANALYZE` to update statistics (done by `rds-migrate maintain --analyze`)

3. Monitor bloat over the next days using:
   ```sql
   SELECT schemaname, relname, n_dead_tup, n_live_tup,
          round(n_dead_tup::numeric / GREATEST(n_live_tup, 1) * 100, 2) as dead_pct
   FROM pg_stat_user_tables
   WHERE n_dead_tup > 10000
   ORDER BY n_dead_tup DESC;
   ```

4. If VACUUM FULL is truly needed (>50% bloat), schedule it during a maintenance window with application awareness

5. Consider `pg_repack` as an alternative — it rebuilds tables online without ACCESS EXCLUSIVE locks

---

## 5. Aurora Cluster Endpoint DNS Propagation

**Severity**: Medium (transient connection failures)

**Problem**: After blue-green switchover on Aurora, the cluster endpoints (writer and reader) need DNS propagation. During the 15-30 second window, DNS may resolve to the old instance.

**Symptoms**:
- Intermittent connection failures for 15-30 seconds after switchover
- Application logs show "connection refused" or "timeout" errors
- Errors resolve spontaneously within 30 seconds

**Mitigation**:
1. **Implement connection retry logic** in your application:
   ```python
   # Python example with retry
   import time
   for attempt in range(5):
       try:
           conn = psycopg2.connect(connection_string)
           break
       except psycopg2.OperationalError:
           time.sleep(2 ** attempt)
   ```

2. **Use a connection pool** with health checks (e.g., PgBouncer, RDS Proxy)

3. **Consider RDS Proxy** — it handles failover transparently

4. **Set low DNS TTL** on your application's DNS resolver

---

## 6. Custom Parameter Group Inheritance

**Severity**: Medium (configuration drift)

**Problem**: When creating a blue-green deployment, AWS creates a new parameter group for the green environment. Custom parameters from the blue environment may not be fully copied, especially if they reference instance-class-specific formulas.

**Affected parameters** (common examples):
- `shared_buffers = {DBInstanceClassMemory/4}` — formula-based, usually copied correctly
- `effective_cache_size` — may need adjustment for PG 16 defaults
- `work_mem` — PG 16 changes default from 4MB to 4MB (unchanged, but verify)
- `maintenance_work_mem` — verify after migration

**Mitigation**:
1. Document all custom parameter values before migration
2. After switchover, compare the old and new parameter groups:
   ```bash
   aws rds describe-db-parameters --db-parameter-group-name old-pg13-params > old-params.json
   aws rds describe-db-parameters --db-parameter-group-name new-pg16-params > new-params.json
   diff <(jq '.Parameters[] | select(.Source == "user") | {(.ParameterName): .ParameterValue}' old-params.json) \
        <(jq '.Parameters[] | select(.Source == "user") | {(.ParameterName): .ParameterValue}' new-params.json)
   ```

---

## 7. Extension Version Mismatches

**Severity**: Medium (may block upgrade)

**Problem**: Some extensions available on PG 13 may not have compatible versions on PG 16 in your AWS region, or may require explicit upgrade steps.

**Commonly affected extensions**:
- `postgis` — usually needs a version bump; check `SELECT postgis_full_version();`
- `pg_partman` — partition management may need schema updates
- `pglogical` — verify compatibility with PG 16 replication changes
- `pg_cron` — usually compatible but verify

**Detection**:
```sql
-- List installed extensions and versions
SELECT extname, extversion FROM pg_extension ORDER BY extname;
```

**Fix**: The blue-green deployment process handles extension upgrades automatically in most cases, but verify post-migration:
```sql
-- After migration, check versions
SELECT extname, extversion FROM pg_extension ORDER BY extname;
-- Update if needed
ALTER EXTENSION postgis UPDATE;
```

---

## 8. Sequence Values and Identity Columns

**Severity**: Low (rare but confusing)

**Problem**: In rare cases, sequence values may appear to "jump" after blue-green switchover. This is because the green environment's sequences are initialized from the replication state, and any transactions in flight during switchover may cause sequence gaps.

**Impact**: Cosmetic only — sequence gaps do not cause data loss or application errors, but may confuse monitoring that tracks sequence progression.

**Mitigation**: No action needed. This is expected PostgreSQL behavior with sequences.

---

## Summary Table

| Gotcha | Severity | Detection | Automated Fix |
|--------|----------|-----------|---------------|
| `max_logical_replication_workers` | Critical | `preflight` command | Yes (param removal) |
| SSL enforcement changes | High | Manual connection test | No (app config change) |
| Cold cache after switchover | High | Expected behavior | Partial (`maintain --analyze`) |
| VACUUM FULL danger | High | N/A (operational) | N/A (use `maintain` instead) |
| Aurora DNS propagation | Medium | Expected behavior | No (app retry logic) |
| Parameter group inheritance | Medium | Manual comparison | Planned |
| Extension version mismatches | Medium | `preflight` command | Partial |
| Sequence value jumps | Low | N/A (cosmetic) | N/A |
