-- A way to clear a broken claim off the screen without pretending it never ran.
--
-- `jobs` is the record of what was actually attempted, so the only honest way
-- to make a failed row stop reading as broken is to mark that one job
-- acknowledged, not to delete it or paper over its state. `_claim_jobs` in
-- views.py filters this column out when it builds the `broke` map; a job with
-- it set is exactly as present in the table as one without.
--
-- Nullable, default NULL, so every existing job is undismissed until someone
-- acts on it, which is the only correct reading of history recorded before
-- this column existed.

ALTER TABLE jobs ADD COLUMN dismissed_at INTEGER;
