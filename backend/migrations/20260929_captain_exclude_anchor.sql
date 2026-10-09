-- Additive migration: record WHICH trigger created a captain report.
--
-- `exclude_anchor` distinguishes a wake-word trigger (the anchor IS the wake
-- phrase, and must stay out of the analysis window) from a button / high-water
-- mark trigger (the anchor is ordinary discussion content, and dropping it
-- silently loses the newest thing said).
--
-- Why a migration and not just `create_all()`: `create_all()` only creates
-- MISSING TABLES. It never ALTERs an existing one, so a database that already
-- has `captain_report` (any local dev DB from ADR-002 Phase 2) would come up
-- without this column and every insert would fail. `docs/decisions/
-- ADR-003` already records this exact hazard for its own metrics fields.
--
-- Safe to run repeatedly. On a fresh DB the table does not exist yet and the
-- CREATE TABLE path (via create_all) will include the column; the
-- `ADD COLUMN` is then a no-op because of the guard below.
--
-- NOTE: SQLite has no `ADD COLUMN IF NOT EXISTS`, so this is applied by
-- `scripts/apply_captain_exclude_anchor.py`, which checks `PRAGMA table_info`
-- first. The statements below document the change; run the script, not this
-- file, against a live SQLite database.

PRAGMA foreign_keys = ON;

-- Existing rows predate the distinction. Defaulting them to 1 (exclude) keeps
-- the STRICTER behaviour: a wake phrase can never leak into a report window,
-- whereas defaulting to 0 could turn a historical wake phrase into content.
ALTER TABLE captain_report
    ADD COLUMN exclude_anchor BOOLEAN NOT NULL DEFAULT 1;
