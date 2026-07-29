# RAW v2 envelope migration

Revision `20260729_0002` adds four envelope columns to `market_events` in one
`ALTER TABLE` statement. Existing rows receive `schema_version=1`; their payload,
legacy sequence, timestamps, source, topic, and symbol are unchanged.

The migration sets a five-second transaction-local lock timeout and fails safely
instead of waiting indefinitely behind an active writer. PostgreSQL 16 stores the
constant default as metadata, so this change does not require a heap rewrite.

No index is created by the migration. Legacy rows have null connection/local
sequence values, and an ordinary transactional index would block RAW writes.
Before proposing an index, measure a representative v2 population and query plan.
If justified, use a separately reviewed operator-run partial concurrent index:

```sql
CREATE INDEX CONCURRENTLY ix_market_events_connection_local_sequence_v2
ON market_events (connection_id, local_sequence)
WHERE connection_id IS NOT NULL AND local_sequence IS NOT NULL;
```

This statement is not run by Alembic or by the application. It requires separate
disk/WAL capacity review and explicit operator approval.

Downgrade removes only the four envelope columns and therefore discards v2
envelope metadata. It does not rewrite RAW payloads. A production downgrade
requires a separate reviewed rollback decision; offline SQL generation or an
isolated test-database downgrade does not authorize production use.

Before a future production rollout, run the read-only aggregate preflight from
the protected deployment environment:

```sh
scripts/raw_v2_preflight.sh
```

It requires the same `HIBACHI_DEPLOY_DIR` and `HIBACHI_RUNTIME_ENV` names as the
operations interface. It verifies PostgreSQL and collector health and reports
only PostgreSQL major/version number, exact bounded RAW row count, heap/index
sizes, active transactions, relation/waiting locks, and free disk. The count has
a five-second statement timeout. The script never selects payloads or prints
runtime configuration, connection strings, query text, or container identifiers.
