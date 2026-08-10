-- Qobuz purchase keys gained the line number they always needed.
--
-- A Qobuz download link is /account/download/{order}/{line}: the order is the
-- transaction and the line is the item within it. The reader used to match
-- only line 1 and record the order on its own as the key, so five tracks
-- bought together were enumerated as one purchase and the other four were
-- never seen. It now reads every line and keys each purchase "{order}/{line}".
--
-- Every key already recorded came from a line 1 link, because that is the only
-- shape the old reader matched, so appending "/1" restates what those rows
-- already mean rather than guessing at them. Doing nothing is the option that
-- would corrupt: /api/purchases and the purchase sweep both recognise an
-- already-filed purchase by this exact string, so a key left in the old shape
-- would stop matching itself and the track would be offered again as new.
--
-- Only Qobuz. `purchased_via` names the store, and another store's key format
-- means nothing here.

UPDATE tracks
   SET purchased_item_key = purchased_item_key || '/1'
 WHERE purchased_via = 'qobuz'
   AND purchased_item_key IS NOT NULL
   AND purchased_item_key NOT LIKE '%/%';
