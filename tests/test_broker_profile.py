"""The profile this app publishes for the Cookie Broker extension.

The rules asserted here are enforced in the extension, in JavaScript, and there
is no way for this repo to run that validator. So they are restated: a
published profile that the extension refuses is a connect button that does
nothing, and the failure would surface in a browser rather than here.

If the extension's rules change, these are the assertions that have to move
with them. The list is in that project's docs/PROTOCOL.md under "What the
extension refuses".
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

WELL_KNOWN = "/.well-known/cookie-broker.json"


class Base(unittest.TestCase):
    token = "test-token"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for key, value in {
            "LW_CONFIG_DIR": str(self.tmp),
            "LW_MUSIC_DIR": str(self.tmp / "music"),
            "LW_LOG_LEVEL": "CRITICAL",
            "LW_RESCAN_CMD": "",
        }.items():
            os.environ[key] = value
        if self.token:
            os.environ["COOKIE_BROKER_TOKEN"] = self.token
        else:
            os.environ.pop("COOKIE_BROKER_TOKEN", None)

        from libwish.settings import Settings
        from libwish.web.app import create_app
        self.app = create_app(Settings.from_env(), start_workers=False)
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("COOKIE_BROKER_TOKEN", None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Published(Base):
    def profile(self):
        res = self.client.get(WELL_KNOWN)
        self.assertEqual(res.status_code, 200, res.data[:200])
        return res.get_json()

    def test_it_is_readable_without_the_token(self):
        # The document explains what the token is for, so requiring the token
        # to read it would put the explanation behind the thing it explains.
        # It carries no secret: a list of shops, and an address the reader
        # already typed.
        res = self.client.get(WELL_KNOWN)
        self.assertEqual(res.status_code, 200)

    def test_it_speaks_protocol_one(self):
        self.assertEqual(self.profile()["protocol"], 1)

    def test_it_does_not_name_its_own_address(self):
        # The extension uses the address it fetched from. Naming one here
        # would make the document wrong for every other deployment.
        self.assertNotIn("base", self.profile()["receiver"])

    def test_endpoints_are_paths_not_addresses(self):
        # The extension refuses a full address here, because a profile that
        # could name a host would be able to redirect a cookie jar away from
        # the origin the reader consented to.
        receiver = self.profile()["receiver"]
        for field in ("ingest", "status"):
            self.assertTrue(receiver[field].startswith("/"), receiver[field])
            self.assertNotIn("..", receiver[field])

    def test_every_site_is_one_the_extension_would_accept(self):
        for site in self.profile()["sites"]:
            with self.subTest(site=site["id"]):
                domain = site["cookieDomain"]
                self.assertFalse(domain.startswith("."), domain)

                # cookieUrl has to be inside cookieDomain, or the name shown
                # to a reader is not the account whose cookies are collected.
                host = urlsplit(site["cookieUrl"]).hostname
                self.assertTrue(
                    host == domain or host.endswith(f".{domain}"),
                    f"{host} is not part of {domain}",
                )

                # The sign-in page is opened in a real tab at the moment
                # someone expects to type a password.
                if site.get("signInUrl"):
                    parts = urlsplit(site["signInUrl"])
                    self.assertEqual(parts.scheme, "https", site["signInUrl"])
                    self.assertTrue(
                        parts.hostname == domain or parts.hostname.endswith(f".{domain}"),
                        f"{parts.hostname} is not part of {domain}",
                    )

                for field in ("id", "label", "requiredCookie"):
                    self.assertTrue(site.get(field), f"{field} is empty")
                self.assertNotIn(" ", site["requiredCookie"])

    def test_it_advertises_only_sites_this_process_can_receive(self):
        from libwish.web.app import COOKIE_SITES
        svc = self.app.extensions["libwish"]
        keepers = set(getattr(svc, "keepers", {}) or {})
        advertised = {s["id"] for s in self.profile()["sites"]}
        # A site advertised with no keeper behind it installs cleanly and then
        # fails on every push, which sends the reader looking in their browser
        # for a problem that is in this app's configuration.
        self.assertEqual(advertised, keepers & set(COOKIE_SITES))


class NotConfigured(Base):
    token = ""

    def test_nothing_is_published_without_an_ingest_endpoint(self):
        # Without COOKIE_BROKER_TOKEN there is no /auth/ingest to push to, so
        # advertising a profile would be advertising an endpoint that is not
        # there.
        self.assertEqual(self.client.get(WELL_KNOWN).status_code, 404)
        self.assertEqual(self.client.post("/auth/ingest/qobuz").status_code, 404)


if __name__ == "__main__":
    unittest.main()
