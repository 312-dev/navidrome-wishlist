"""Store provider behaviour that does not need a live account.

The cases here are the ones where a provider has to tell two situations apart
that look identical from the outside, because getting one of those wrong is
not a crash: it is the application confidently reporting the wrong fact about
someone's purchases.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from libwish.errors import StoreAuthError
from libwish.http import Response
from libwish.stores.qobuz import QobuzStore

# What the account page looks like signed out: a redirect to the sign-in form,
# answered 200, whose body says nothing about signing in until far past any
# window worth scanning. Measured against the live page, where the first
# mention of the sign-in path is around 75,000 characters in.
SIGNIN_URL = "https://www.qobuz.com/signin"
SIGNIN_BODY = ("<!doctype html><html><head><title>Qobuz</title></head><body>"
               + "<div>" * 4000 + "<a href='/signin'>Sign in</a></body></html>")

DOWNLOADS_URL = "https://www.qobuz.com/profile/downloads/track"


def a_store(response: Response) -> QobuzStore:
    """A Qobuz provider whose every request answers with `response`.

    The client is injected rather than built, because the real one is reached
    through a credential handle and this has nothing to say about credentials.
    """
    store = QobuzStore(SimpleNamespace(log=SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None)))
    store._http = SimpleNamespace(get=lambda *a, **k: response)
    return store


class SignedOutIsNotAnEmptyAccount(unittest.TestCase):
    """Signed out and owning nothing are different facts.

    Reporting the first as the second is what made a claim refuse with "Qobuz
    reports no purchases at all" against an account that had plenty, and would
    tell a reader picking a purchase by hand that there was nothing to pick.
    """

    def test_a_signed_out_response_is_recognised_by_where_it_came_from(self):
        store = a_store(Response(200, SIGNIN_URL, SIGNIN_BODY.encode()))
        with self.assertRaises(StoreAuthError) as caught:
            list(store.list_owned())
        self.assertEqual(caught.exception.code, "signed_out")

    def test_the_body_alone_would_not_have_told_us(self):
        # The guard this replaces read the first 3000 characters of the body.
        # Asserted here so the test fails loudly if a future change goes back
        # to scanning content, rather than silently passing on a fixture that
        # happens to mention the form early.
        self.assertNotIn("/signin", SIGNIN_BODY[:3000])

    def test_signed_out_is_reported_as_reachable_but_not_authed(self):
        store = a_store(Response(200, SIGNIN_URL, SIGNIN_BODY.encode()))
        health = store.check()
        self.assertTrue(health.ok)
        self.assertFalse(health.authed)
        self.assertIsNone(health.owned_count)

    def test_an_account_page_with_no_purchases_is_not_read_as_signed_out(self):
        # The other direction, and the reason this cannot simply search the
        # whole body: a signed-in page is free to link to the sign-in form.
        body = "<html><body>No downloads yet. <a href='/signin'>Sign in</a></body></html>"
        store = a_store(Response(200, DOWNLOADS_URL, body.encode()))
        health = store.check()
        self.assertTrue(health.authed)
        self.assertEqual(health.owned_count, 0)
        self.assertEqual(list(store.list_owned()), [])


if __name__ == "__main__":
    unittest.main()
