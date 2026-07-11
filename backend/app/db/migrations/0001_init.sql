-- Marks the framework baseline. app/db/__init__.py creates the
-- schema_migrations tracking table itself (it has to exist before this file
-- can even be recorded as applied); later phases add their own numbered
-- files here for real tables (docs metadata, provider registry, config,
-- metric counters).
SELECT 1;
