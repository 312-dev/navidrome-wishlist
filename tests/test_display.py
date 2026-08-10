"""What reaches a reader's eyes, as opposed to what is true underneath.

An audit row and a screen want different things out of the same object, and
the failure this guards against is not a crash: it is a Python repr appearing
in a panel where the name of a record the reader nearly bought should be.
"""

from __future__ import annotations

import json
import unittest

from libwish import identity
from libwish.claim import _describe
from libwish.web.views import _plain, _prose


class RecordedIdentitiesStayReadable(unittest.TestCase):
    def setUp(self):
        self.ident = identity.build_identity("CHVRCHES", "Lies")

    def test_a_nested_identity_survives_as_data_not_as_a_repr(self):
        payload = json.loads(json.dumps(_describe(self.ident), default=str))
        # The normalised title is a structure of its own. Serialised without
        # recursion it becomes "NormalizedTitle(base='lies', ...)".
        self.assertIsInstance(payload["title"], dict)
        self.assertNotIn("NormalizedTitle", json.dumps(payload))

    def test_the_panel_reads_the_string_a_store_printed(self):
        payload = json.loads(json.dumps(_describe(self.ident), default=str))
        self.assertEqual(_plain(payload, "artist"), "CHVRCHES")
        self.assertEqual(_plain(payload, "title"), "Lies")

    def test_a_structure_is_refused_rather_than_stringified(self):
        # No raw field to fall back to, and the normalised sibling is not
        # text. A blank says less than a repr, which is the point.
        self.assertEqual(_plain({"title": {"base": "lies"}}, "title"), "")

    def test_an_absent_candidate_reads_as_nothing_recorded(self):
        self.assertEqual(_describe(None), {})
        self.assertEqual(_plain({}, "artist"), "")


class OldRowsDoNotShout(unittest.TestCase):
    """Rows recorded before `reasons` held prose still hold a gate code.

    Those rows are on disk and cannot be rewritten honestly, so the panel has
    to recognise one and say nothing rather than print it where a sentence
    belongs.
    """

    def test_a_gate_code_is_not_shown_as_an_explanation(self):
        for code in ("ARTIST_MISMATCH", "TITLE_MISMATCH", "NO_CANDIDATES"):
            self.assertEqual(_prose(code), "", code)

    def test_a_real_sentence_survives(self):
        said = "The closest purchase is by CHVRCHES, not Audioslave."
        self.assertEqual(_prose(said), said)

    def test_a_sentence_that_names_a_gate_is_still_a_sentence(self):
        # Upper case appears in prose. Only the shape of a bare code is
        # dropped, so a sentence mentioning one is not swallowed with it.
        said = "The gate ARTIST_MISMATCH is why this was refused."
        self.assertEqual(_prose(said), said)

    def test_nothing_recorded_reads_as_nothing(self):
        self.assertEqual(_prose(None), "")
        self.assertEqual(_prose("   "), "")


if __name__ == "__main__":
    unittest.main()
