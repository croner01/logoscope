# Database Profile Runbook (SINGLE / HA)

Related release record (Redmine):

- `docs/operations/redmine-db-profile-single-ha-2026-03-04.md`

## Scope

This runbook defines operations for two database profiles:

- `single`:
  - ClickHouse `Deployment(1)` + `MergeTree*`
  - Redis `Deployment(1)`
- `ha`:
  - ClickHouse `StatefulSet(3)` + ClickHouse Keeper `StatefulSet(3)` + `Replicated*MergeTree`
  - Redis `StatefulSet(3)` (1 master + 2 replicas)

Profile switching:

- deploy.sh: `DB_PROFILE=single|ha`
- control scripts: `DB_PROFILE=single|ha|auto`
- Helm values: `database.profile: single|ha`

## Deployment Manifests

- `deploy/clickhouse-single.yaml`
- `deploy/clickhouse-ha.yaml`
- `deploy/redis-single.yaml`
- `deploy/redis-ha.yaml`
- `charts/logoscope/files/manifests/clickhouse-single.yaml`
- `charts/logoscope/files/manifests/clickhouse-ha.yaml`
- `charts/logoscope/files/manifests/redis-single.yaml`
- `charts/logoscope/files/manifests/redis-ha.yaml`

## Control Scripts

- `scripts/clickhouse-ha-control.sh`
- `scripts/redis-ha-control.sh`
- `scripts/db-ha-control.sh`

## ClickHouse Operations

### 1. Initialize replicated schema

```bash
DB_PROFILE=ha NAMESPACE=islap scripts/clickhouse-ha-control.sh bootstrap
```

### 2. Consistency check

```bash
DB_PROFILE=ha NAMESPACE=islap scripts/clickhouse-ha-control.sh check
```

Checks include:

- `system.replicas.is_readonly = 0`
- `queue_size <= CLICKHOUSE_MAX_QUEUE_SIZE` (default `200`)
- `absolute_delay <= CLICKHOUSE_MAX_ABSOLUTE_DELAY` (default `120s`)

### 3. Replica sync with retries

```bash
DB_PROFILE=ha NAMESPACE=islap scripts/clickhouse-ha-control.sh sync
```

Retry controls:

- `SYNC_RETRIES` (default `3`)
- `SYNC_RETRY_SLEEP` (default `3s`)

### 4. Rolling restart strategy

```bash
DB_PROFILE=ha NAMESPACE=islap scripts/clickhouse-ha-control.sh rolling-restart
```

Strategy:

1. Restart one replica at a time.
2. Wait pod ready after each restart.
3. Run partial sync/check before moving to next replica.

## Redis Operations

### 1. Topology/consistency check

```bash
DB_PROFILE=ha NAMESPACE=islap scripts/redis-ha-control.sh check
```

Checks include:

- service-selected master pod role is `master`
- non-master pods role is `slave`
- `master_link_status = up`
- replication lag <= `REDIS_MAX_REPLICA_LAG_BYTES` (default `1048576`)

### 2. Failover / promote replica to master

```bash
DB_PROFILE=ha NAMESPACE=islap scripts/redis-ha-control.sh promote redis-1
```

Actions:

1. `REPLICAOF NO ONE` on promoted pod.
2. Repoint other replicas to new master.
3. Patch `redis` Service selector to new master pod.

### 3. Rolling restart strategy

```bash
DB_PROFILE=ha NAMESPACE=islap scripts/redis-ha-control.sh rolling-restart
```

Strategy:

1. Restart replicas first.
2. Restart master last.
3. Run replication check after each restart.

## Unified Operations

```bash
# Global status
DB_PROFILE=ha NAMESPACE=islap scripts/db-ha-control.sh status

# Global consistency checks
DB_PROFILE=ha NAMESPACE=islap scripts/db-ha-control.sh check

# Promote redis replica
DB_PROFILE=ha NAMESPACE=islap scripts/db-ha-control.sh promote-redis redis-2
```

## Single Profile Quick Checks

```bash
DB_PROFILE=single NAMESPACE=islap scripts/clickhouse-ha-control.sh bootstrap
DB_PROFILE=single NAMESPACE=islap scripts/clickhouse-ha-control.sh check
DB_PROFILE=single NAMESPACE=islap scripts/redis-ha-control.sh check
```

## Production Change Sequence (Recommended)

1. Apply manifests for ClickHouse/Redis.
2. Wait StatefulSets ready.
3. Run ClickHouse bootstrap.
4. Run global checks.
5. Shift application traffic / start write path.
6. Observe lag and replica queues for at least one retention cycle.

## Rollback Guidance

- If ClickHouse replication health fails: stop write-heavy jobs, run `sync`, then evaluate pod-level issues.
- If Redis failover misroutes traffic: run `promote <current-good-pod>` and re-check.
- Avoid simultaneous restart of all replicas.
