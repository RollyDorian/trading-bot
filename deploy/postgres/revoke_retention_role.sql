-- Revoke retention mutation privileges and optionally drop the login role.
-- Run as the database/schema owner.

\set ON_ERROR_STOP on

-- Must match the database used at provision time.
\set retention_db 'cryptobot'
\set retention_schema 'public'

REVOKE ALL ON TABLE :"retention_schema".market_events FROM retention;
REVOKE ALL ON SCHEMA :"retention_schema" FROM retention;
REVOKE CONNECT ON DATABASE :"retention_db" FROM retention;

-- Uncomment after confirming no active retention sessions:
-- DROP ROLE IF EXISTS retention;
