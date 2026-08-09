-- Revoke the partition maintenance identity. Does not DROP partitions.
\set ON_ERROR_STOP on
\set maintenance_db 'cryptobot'
\set maintenance_schema 'public'

REVOKE ALL ON FUNCTION
    public.drop_verified_market_event_generation(text, text, boolean)
    FROM partition_maintenance;
REVOKE ALL ON TABLE :"maintenance_schema".market_event_generations FROM partition_maintenance;
REVOKE ALL ON TABLE :"maintenance_schema".market_events FROM partition_maintenance;
REVOKE ALL ON SCHEMA :"maintenance_schema" FROM partition_maintenance;
REVOKE ALL ON DATABASE :"maintenance_db" FROM partition_maintenance;

-- Optional: DROP ROLE partition_maintenance;
