"""Store provider behaviour that does not need a live account.

The cases here are the ones where a provider has to tell two situations apart
that look identical from the outside, because getting one of those wrong is
not a crash: it is the application confidently reporting the wrong fact about
someone's purchases.
"""

from __future__ import annotations

import html as H
import unittest
from types import SimpleNamespace

from libwish.errors import StoreAuthError
from libwish.http import Response
from libwish.stores.qobuz import DOWNLOADS_PATH, QobuzStore

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
    store.requested = []
    store._http = SimpleNamespace(
        get=lambda path, *a, **k: (store.requested.append(path), response)[1])
    return store


def a_row(order: str, line: str, title: str, album: str, artist: str,
          quality: str = "Hi-Res", date: str = "8/10/26") -> str:
    """One purchase, in the shape the us-en storefront renders it.

    Transcribed from the live downloads page and reduced to the elements that
    carry a field, with the names replaced. The structure is what is being
    asserted against, so it is kept exactly: the class names, the `title`
    attribute repeating the link's own text, the album and the performer as
    separate links, and the download link last.
    """
    esc = lambda text: H.escape(text, quote=True)
    return f"""
            <div class="account-purchases__table-row">
                <div class="album-cover__image-container">
                    <a href="/us-en/album/an-album/abc123">
                        <img class="album-cover__image" src="cover.jpg" />
                    </a>
                    <div class="account-purchases__name">
                        <span class="table-header table-header--name"> Title</span>
                        <span class="account-purchases__album-title">
                            <a class="account-purchases__track-title--link"
                               href="/us-en/album/an-album/abc123" title="{esc(title)}">
                                {esc(title)}
                            </a>
                        </span>
                        <a class="account-purchases__track-title--link account-purchases__track--favorites"
                           href="/us-en/album/an-album/abc123">
                            {esc(album)}
                        </a>
                        <span class="account-purchases__album-artist">
                            <a class="artist account-purchases__track-title--link"
                               href="/us-en/interpreter/someone/1" title="{esc(artist)}">
                                {esc(artist)}
                            </a>
                        </span>
                    </div>
                </div>
                <div class="account-purchases__date--container">
                    <span class="table-header table-header--quality"> Quality</span>
                    <span class="account-purchases__date">{quality}</span>
                </div>
                <div class="account-purchases__order-container">
                    <span class="table-header table-header--date">Date</span>
                    <span class="account-purchases__date">{date}</span>
                    <span class="table-header table-header--order">Order #</span>
                    <span class="account-purchases__order">{order}</span>
                </div>
                <a class="action-column" href="/account/download/{order}/{line}">
                    <button class="ButtonPrimary ButtonPrimary--small">Download</button>
                </a>
            </div>
"""


def a_listing(*rows: str) -> Response:
    page = ('<html><body><div class="account-purchases__table-nav">'
            + "".join(rows) + "</div></body></html>")
    return Response(200, DOWNLOADS_URL, page.encode())


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


class OneOrderIsNotOnePurchase(unittest.TestCase):
    """Five tracks bought together are five purchases, not one.

    A Qobuz download link is /account/download/{order}/{line}. The reader used
    to match only `/1`, which meant an order of five tracks was enumerated as
    its first line and the other four were never reported at all: they did not
    turn up as unmatched or as near misses, they were simply absent, and the
    sweep answered "nothing new to file" about purchases made minutes earlier.
    """

    # The lines a real five-track order was rendered with. Deliberately not
    # 1..5: the line is an index within the order and the download page lists
    # only track purchases, so the numbering has gaps and cannot be generated.
    ORDER = "68647832"
    LINES = ("1", "2", "3", "5", "6")
    TITLES = ("Apologize", "Stop And Stare", "Doom And Gloom",
              "Radar Love (Remastered)", "Gold Dust Woman (2004 Remaster)")

    def listing(self) -> Response:
        return a_listing(*[
            a_row(self.ORDER, line, title, "An Album", "An Artist")
            for line, title in zip(self.LINES, self.TITLES)
        ])

    def test_every_track_in_the_order_is_enumerated(self):
        store = a_store(self.listing())
        self.assertEqual([i.title for i in store.list_owned()], list(self.TITLES))

    def test_each_one_is_keyed_by_order_and_line_together(self):
        # Keyed on the order alone, these five collide, and the filed-already
        # check would treat the whole order as filed once any one of them was.
        store = a_store(self.listing())
        keys = [i.item_key for i in store.list_owned()]
        self.assertEqual(keys, [f"{self.ORDER}/{line}" for line in self.LINES])
        self.assertEqual(len(set(keys)), len(keys))

    def test_the_listing_is_read_in_one_request(self):
        # The fields used to be recovered by fetching each purchase's options
        # page for its album name, so enumerating cost a request per row. The
        # listing states all of it.
        store = a_store(self.listing())
        list(store.list_owned())
        self.assertEqual(store.requested, [DOWNLOADS_PATH])

    def test_the_account_page_counts_every_line(self):
        store = a_store(self.listing())
        self.assertEqual(store.check().owned_count, len(self.LINES))


class FieldsComeFromTheirOwnLabels(unittest.TestCase):
    """The row names each field; none of them is recovered by splitting text.

    The listing renders the track, the album and the performer as three
    adjacent links with nothing between them, so reading them as one run of
    names and cutting it on the album is what the reader used to do. That is
    how a request for CHVRCHES "Lies" once collected a track whose title only
    mentions the word in a tie-in credit.
    """

    def one(self, **kw) -> "OwnedItem":
        store = a_store(a_listing(a_row(**kw)))
        items = list(store.list_owned())
        self.assertEqual(len(items), 1)
        return items[0]

    def test_the_title_is_the_track_and_not_the_album(self):
        item = self.one(order="1", line="1", title="Lies",
                        album="The Bones of What You Believe", artist="CHVRCHES")
        self.assertEqual(item.title, "Lies")
        self.assertEqual(item.release_title, "The Bones of What You Believe")
        self.assertEqual(item.artist, "CHVRCHES")

    def test_a_title_carrying_a_quoted_credit_survives_intact(self):
        # The exact title that made this worth being careful about. It arrives
        # escaped inside an attribute, and a reader that stopped at the first
        # quote would truncate it to `Such Great Heights (From `.
        title = 'Such Great Heights (From "Tell Me Lies Season 3")'
        item = self.one(order="1", line="1", title=title,
                        album="Such Great Heights", artist="CHVRCHES")
        self.assertEqual(item.title, title)

    def test_a_performer_is_read_even_where_the_title_is_a_paragraph(self):
        # Classical. The options page names the composer while the row names
        # the performer, which is why recovering the artist by splitting on a
        # known album needed a hint and sometimes failed outright.
        long_title = ("Beethoven: Piano Sonata No. 30 in E Major, Op. 109: "
                      "I. Vivace ma non troppo – Adagio espressivo – "
                      "(Live at Suntory Hall, Tokyo, 2025)")
        item = self.one(order="1", line="1", title=long_title,
                        album="Beethoven: Piano Sonatas, Opp. 109, 110 & 111",
                        artist="Mitsuko Uchida")
        self.assertEqual(item.title, long_title)
        self.assertEqual(item.artist, "Mitsuko Uchida")
        self.assertEqual(item.release_title,
                         "Beethoven: Piano Sonatas, Opp. 109, 110 & 111")

    def test_hi_res_is_offered_as_flac_only(self):
        # An MP3 of a hi-res purchase is not what was bought.
        hires = self.one(order="1", line="1", title="T", album="A", artist="B",
                         quality="Hi-Res")
        cd = self.one(order="1", line="2", title="T", album="A", artist="B",
                      quality="CD")
        self.assertEqual(hires.formats, ("flac",))
        self.assertEqual(cd.formats, ("flac", "mp3"))


class KeysRecordedBeforeTheLineWasRead(unittest.TestCase):
    def test_a_bare_order_is_read_as_its_first_line(self):
        # Only `/1` links were ever matched, so a key with no line is line 1.
        # Guessing anything else would download a different track than the one
        # that was filed.
        from libwish.models import OwnedItem

        item = OwnedItem(store="qobuz", item_key="68471357", kind="track",
                         title="Lies", artist="CHVRCHES", release_title="")
        self.assertEqual(QobuzStore._key_parts(item), ("68471357", "1"))

    def test_the_enumerated_pair_is_preferred_over_the_key(self):
        from libwish.models import OwnedItem

        item = OwnedItem(store="qobuz", item_key="68647832/6", kind="track",
                         title="Apologize", artist="OneRepublic", release_title="",
                         raw={"order": "68647832", "line": "6"})
        self.assertEqual(QobuzStore._key_parts(item), ("68647832", "6"))


if __name__ == "__main__":
    unittest.main()
