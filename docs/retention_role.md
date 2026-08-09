# PostgreSQL retention role

## Purpose

Production bounded RAW deletion mutates `market_events` only through a dedicated
least-privilege login role named `retention`. Collector, exporter, dashboard, and
health checks continue to use the `research` role, which must never receive
`DELETE`, `UPDATE`, or `TRUNCATE` on `market_events`.

## Privilege matrix

| Role | CONNECT | USAGE on schema | `market_events` SELECT | UPDATE | DELETE | INSERT | TRUNCATE | Other tables |
|---|---|---|---|---|---|---|---|---|
| `research` / `cryptobot_runtime` | yes | yes | yes | no | no | yes | no | insert/select as today |
| `retention` | yes | yes (typically `public`) | yes | yes* | yes | no | no | none |

\* `UPDATE` is required only because bounded deletion uses
`SELECT ... FOR UPDATE SKIP LOCKED` for row locking. The retention executor never
issues `UPDATE` DML.

`retention` is a login role, not owner, not superuser. Schema DDL, sequence
ownership, trigger/reference privileges, and grants on any table other than
`market_events` are prohibited.

## Credential isolation

| Command | Database URL | Role expectation |
|---|---|---|
| `retention-coverage-gate` | `DATABASE_URL` (`research`) | read-only planning |
| `retention-dry-run` | `DATABASE_URL` (`research`) | `COUNT(*)` only |
| `retention-execute` without `--confirm-delete` | `DATABASE_URL` (`research`) | dry-run only |
| `retention-execute --confirm-delete` | `RETENTION_DATABASE_URL` only | must authenticate as `retention` |

`RETENTION_DATABASE_URL` is a `postgresql+asyncpg` URL stored only in the
protected operator runtime environment. The CLI fails closed when mutation is
requested without it and never silently falls back to `DATABASE_URL`.

## Identity gate

Before the first delete batch, `BoundedRetentionRunner.execute` calls
`require_retention_mutation_identity(session)` when `confirm_delete=True` and
`test_mode=False`. The helper queries:

```sql
SELECT current_user, session_user, current_setting('is_superuser')::boolean
```

Mutation is rejected when:

- `current_user` is not exactly `retention`;
- `current_user` is in the forbidden set (`research`, `cryptobot`,
  `cryptobot_runtime`, `postgres`, `test`, and documented owner/superuser
  shortcuts);
- `session_user` is not `retention` (blocks `SET ROLE` / owner shortcuts);
- `is_superuser` is true.

Errors name the observed role only; credentials are never logged.

## Operation id policy

Failed privilege-canary audits must remain `failed`. Do not rewrite a failed
record to `pass`.

The privilege-canary audit `c146073f-faeb-47c1-bc86-d71ae9c73d97` failed with
zero rows deleted and must be preserved as failed evidence.

The next approved production canary must pass a **new** `--operation-id`. When
progress status is `failed` and `cumulative_deleted == 0`, `retention-execute
--confirm-delete` refuses to resume that operation id and instructs the operator
to choose a new one.

## Provisioning and rollback

Operator-only SQL lives outside Alembic:

- `deploy/postgres/provision_retention_role.sql` — idempotent grants
- `deploy/postgres/revoke_retention_role.sql` — revoke and optional drop

Set the login password interactively as the database owner, for example:

```text
\password retention
```

Never commit passwords or `RETENTION_DATABASE_URL` values to Git.

Rollback order:

1. Stop any in-flight retention execute command.
2. Run `deploy/postgres/revoke_retention_role.sql`.
3. Remove `RETENTION_DATABASE_URL` from the protected runtime environment.
4. Preserve audit/progress JSON for the failed or partial operation.

## Canary re-approval flow

1. Provision or verify the `retention` role and `RETENTION_DATABASE_URL`.
2. Run `retention-coverage-gate` and `retention-dry-run` with `research`.
3. Obtain separate human approval for production mutation.
4. Start `retention-execute` with a **new** `--operation-id`, confirmed guards,
   archive coverage gate, and `--confirmation-token DELETE_VERIFIED_ARCHIVE`.
5. If privilege or identity checks fail, leave the audit `failed` and fix grants
   before retrying with another new operation id.
