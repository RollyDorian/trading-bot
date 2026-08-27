-- Least-privilege maintenance identity for verified RAW generation DROP.
-- Run as the table/schema owner on disposable or reviewed environments only.
-- Do not grant these privileges to research, retention, or collector roles.
--
-- Destructive DROP remains operator-approved. This script only provisions the
-- callable SECURITY DEFINER gate; it does not schedule automatic DROP.

\set ON_ERROR_STOP on
\set maintenance_db 'cryptobot'
\set maintenance_schema 'public'

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_maintenance') THEN
        CREATE ROLE partition_maintenance LOGIN;
    END IF;
END
$$;

REVOKE ALL ON DATABASE :"maintenance_db" FROM partition_maintenance;
REVOKE ALL ON SCHEMA :"maintenance_schema" FROM partition_maintenance;
REVOKE ALL ON ALL TABLES IN SCHEMA :"maintenance_schema" FROM partition_maintenance;

GRANT CONNECT ON DATABASE :"maintenance_db" TO partition_maintenance;
GRANT USAGE ON SCHEMA :"maintenance_schema" TO partition_maintenance;

-- Read generation metadata and inspect partition sizes; no broad DML.
GRANT SELECT ON TABLE :"maintenance_schema".market_event_generations TO partition_maintenance;
GRANT SELECT ON TABLE :"maintenance_schema".market_events TO partition_maintenance;

-- Execute only the gated DROP function (SECURITY DEFINER owned by migrator).
GRANT EXECUTE ON FUNCTION
    public.drop_verified_market_event_generation(text, text, boolean)
    TO partition_maintenance;

-- Explicitly withhold DDL / mutation on RAW and generation metadata.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLE :"maintenance_schema".market_events
    FROM partition_maintenance;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLE :"maintenance_schema".market_event_generations
    FROM partition_maintenance;
