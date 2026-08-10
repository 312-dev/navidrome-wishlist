-- A way to clear a refusal off the screen without deleting the record of it.
--
-- `match_decision` is the audit trail of what the software decided about the
-- reader's money, so the only honest way to make a refused row stop reading as
-- refused is to mark the decision acknowledged, not to delete it or overwrite
-- its outcome. `refusal_for` in views.py filters this column out; a row with it
-- set is exactly as present in the table as one without.
--
-- Nullable, default NULL, so every existing decision is undismissed until
-- someone acts on it, which is the only correct reading of history recorded
-- before this column existed.

ALTER TABLE match_decision ADD COLUMN dismissed_at INTEGER;
