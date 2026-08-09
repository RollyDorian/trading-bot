-- Idempotent provisioning for the least-privilege RAW retention role.
-- Run as the database/schema owner. Do not commit passwords to Git.
--
-- After grants succeed, set the login password interactively:
--   \password retention

\set ON_ERROR_STOP on

-- Database name is the PostgreSQL database (not DATABASE_ROLE).
-- This deployment's COLLECT database is typically `cryptobot`.
-- Override before running if needed: \set retention_db 'your_db'
\set retention_db 'cryptobot'
\set retention_schema 'public'

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'retention') THEN
        CREATE ROLE retention LOGIN;
    END IF;
END
$$;

REVOKE ALL ON DATABASE :"retention_db" FROM retention;
REVOKE ALL ON SCHEMA :"retention_schema" FROM retention;
REVOKE ALL ON ALL TABLES IN SCHEMA :"retention_schema" FROM retention;

GRANT CONNECT ON DATABASE :"retention_db" TO retention;
GRANT USAGE ON SCHEMA :"retention_schema" TO retention;

-- UPDATE is required only for SELECT ... FOR UPDATE SKIP LOCKED locking.
GRANT SELECT, UPDATE, DELETE ON TABLE :"retention_schema".market_events TO retention;

-- Explicitly withhold broader mutation privileges.
REVOKE INSERT, TRUNCATE, TRIGGER, REFERENCES
    ON TABLE :"retention_schema".market_events
    FROM retention;
