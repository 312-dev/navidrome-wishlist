-- The store's own identifier for the purchase a claim actually downloaded.
--
-- `purchased_via` already names the store (`mark_purchased` receives it as
-- the store id), so this is the identity within that store: the same
-- `item_key` an `OwnedItem` carries, on both the ordinary matched path and
-- the hand-picked one. Recording it is what lets `/api/purchases/<store_id>`
-- tell a purchase already filed apart from one that is not, instead of
-- offering the reader their own downloaded record back as if it were new.
--
-- Nullable, default NULL: a purchase filed before this column existed has no
-- key to check against, and that is not a guess anything else can fill in.
-- Two different purchases of the same song are a real thing, so matching by
-- artist and title instead would risk hiding a purchase the reader still
-- needs. A row with no key here stays offered forever, which is the only
-- honest reading of a record this column cannot describe.

ALTER TABLE tracks ADD COLUMN purchased_item_key TEXT;
