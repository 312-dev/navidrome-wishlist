"""The claim pipeline: turning "I bought this" into a verified file in the library.

Five phases, named because the interface counts them out to the user: session,
enumerate, match, download, verify. Commit is not a phase; it is the transition
that ends the job.

Two rules shape the whole thing.

The pipeline matches, never the store. A store reports what it found and this
layer decides whether any of it is the track that was asked for. Keeping that
decision in one place is what makes it testable and what keeps a store author
from inventing their own notion of "close enough".

Nothing enters the library unverified. A download lands in staging, is checked
for being the kind of file it claims to be, and only then is renamed into place.
The check exists because a store that has logged you out answers with an HTML
sign-in page and HTTP 200, which written straight to disk is a plausible-looking
file of the right name and the wrong everything.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess
import time
from typing import Any, Sequence

from . import identity, match
from .errors import (
    LibwishError, MatchRefused, StoreAuthError, VerificationFailed,
)
from .log import get
from .models import MATCH_CONFIRM_MIN, OwnedItem
from .paths import FileExists

log = get("claim")

# Formats to ask a store for, best first.
PREFER: tuple[str, ...] = ("flac", "mp3")

# Leading bytes that identify real audio. An HTML error page matches none of
# them, which is the case this exists to catch.
MAGIC = (
    (b"fLaC", "flac"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
    (b"OggS", "ogg"),
)

MIN_BYTES = 100_000


def _describe(ident) -> dict:
    """A JSON-safe summary of an identity, for the audit row.

    Stored rather than referenced because the audit has to stay readable after
    the lexicon changes. A decision explained by today's rules is not
    reconstructable from tomorrow's.
    """
    if ident is None:
        return {}
    if dataclasses.is_dataclass(ident):
        out = {}
        for f in dataclasses.fields(ident):
            value = getattr(ident, f.name, None)
            if isinstance(value, (set, frozenset, tuple)):
                out[f.name] = list(value)
            elif dataclasses.is_dataclass(value):
                # Recursed rather than left to json.dumps, whose `default=str`
                # turns a nested identity into a Python repr. That repr then
                # reads back out as the candidate's title, and the refusal
                # panel showed a reader `NormalizedTitle(base='lies', ...)`
                # where the name of the record they nearly bought should be.
                out[f.name] = _describe(value)
            else:
                out[f.name] = value
        return out
    return {"repr": str(ident)[:500]}


def verify_audio(path, *, min_bytes: int = MIN_BYTES) -> str:
    """Confirm a downloaded file is audio. Returns the detected format.

    Deliberately checks the bytes rather than the extension. The filename comes
    from the store, so trusting it would mean trusting the same source that just
    handed us the file.
    """
    if not path.is_file():
        raise VerificationFailed(f"{path} is not a file")
    size = path.stat().st_size
    if size < min_bytes:
        head = path.read_bytes()[:120].decode("utf-8", "replace")
        raise VerificationFailed(
            f"{path.name} is only {size} bytes, too small to be audio. Starts with: {head!r}"
        )
    with open(path, "rb") as fh:
        head = fh.read(8)
    for magic, fmt in MAGIC:
        if head.startswith(magic):
            return fmt
    raise VerificationFailed(
        f"{path.name} does not begin with any known audio signature "
        f"(first bytes {head[:8]!r}); the store may have returned an error page"
    )


def rescan(command: str) -> None:
    """Tell the music server a new file landed.

    Best effort. The file is already in the library and the purchase is already
    recorded, so a server that declines to rescan is a nuisance rather than a
    reason to fail a claim that succeeded.
    """
    if not command.strip():
        return
    try:
        result = subprocess.run(shlex.split(command), timeout=120,
                                capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("rescan exited %s: %s", result.returncode,
                        (result.stderr or "").strip()[:200])
    except Exception as exc:
        log.warning("rescan failed: %s: %s", type(exc).__name__, exc)


class ClaimPipeline:
    """Runs one claim. Constructed per job so it can carry per-run state."""

    def __init__(self, svc: Any, stores: dict[str, Any]) -> None:
        self.svc = svc
        self.stores = stores

    def __call__(self, job, progress) -> None:
        track = self.svc.tracks.get(job.track_id)
        if track is None:
            raise LibwishError(f"track {job.track_id} no longer exists")
        store = self._store_for(job.provider_id)

        progress("session", store=store.id)
        health = store.check()
        if not health.ok:
            raise StoreAuthError(f"{store.name} is unreachable: {health.detail}",
                                 code="store_down", provider_id=store.id)
        if not health.authed:
            raise StoreAuthError(f"{store.name} is signed out. Open it in your browser "
                                 f"to reconnect.", code="signed_out", provider_id=store.id)

        progress("enumerate", store=store.id, owned=health.owned_count)
        owned: list[OwnedItem] = list(store.list_owned())
        if not owned:
            raise MatchRefused(f"{store.name} reports no purchases at all.",
                               reason="empty_inventory")

        # A hand-picked purchase skips scoring entirely rather than joining the
        # candidate list: someone pointing at one exact item is stronger
        # evidence than any score, and the confirm floor that gates
        # `_user_confirmed` below has no bearing on an item nobody asked the
        # matcher to judge in the first place.
        picked_key = self._user_picked_item(track["id"])
        if picked_key is not None:
            item = next((i for i in owned if i.item_key == picked_key), None)
            if item is None:
                raise LibwishError(
                    f"The purchase you picked is no longer in your {store.name} account.")
            log.info("downloading a hand-picked purchase",
                     context={"track": track["id"], "item": item.item_key})
            # Written so the pick is spent once, the same guarantee
            # `_user_confirmed` gives a confirmation: this row becomes the
            # newest decision for the track, so a later, unrelated claim reads
            # `_user_picked_item` as None instead of silently redownloading
            # today's choice. It also gives the audit the item's real fields,
            # in place of whatever strings the picker's request carried.
            want = identity.build_identity(track["artist"], track["title"])
            self._record(track["id"], want, item, None, "downloaded_by_pick",
                         "downloaded the purchase picked by hand, not matched",
                         len(owned), store.id)
        else:
            progress("match", candidates=len(owned))
            want = identity.build_identity(track["artist"], track["title"])
            candidates = [identity.from_owned_item(item) for item in owned]
            decision, index = self._decide(want, candidates, track, store.id)
            item = owned[index]
            log.info("matched", context={"track": track["id"], "item": item.item_key,
                                         "score": getattr(decision, "score", None)})

        progress("download", title=item.title, artist=item.artist)
        staging = self.svc.paths.staging_dir
        result = store.download(item, staging, PREFER, progress)

        progress("verify", bytes=result.bytes)
        try:
            detected = verify_audio(result.path)
        except VerificationFailed:
            self.svc.paths.discard(result.path)
            raise

        dest = self.svc.paths.library_path(
            item.artist or track["artist"], item.title or track["title"],
            album=item.release_title or "", suffix=result.path.suffix or f".{detected}",
        )
        try:
            self.svc.paths.publish(result.path, dest)
        except FileExists:
            # Already owned. The claim has still achieved its purpose, so the
            # track leaves the queue rather than reporting a failure the user
            # cannot act on.
            self.svc.paths.discard(result.path)
            log.info("already in the library", context={"dest": str(dest)})

        self.svc.tracks.mark_purchased(track["id"], store.id, item.item_key)
        self.svc.bus.publish("track.updated", id=track["id"], status="purchased",
                             store=store.id, path=str(dest))
        rescan(self.svc.settings.rescan_cmd)
        self.svc.bus.publish("scan.requested", path=str(dest.parent))

    def _store_for(self, store_id: str | None):
        if store_id:
            store = self.stores.get(store_id)
            if store is None:
                raise LibwishError(f"store {store_id!r} is not configured")
            return store
        if len(self.stores) == 1:
            return next(iter(self.stores.values()))
        raise LibwishError("more than one store is configured, so a claim must name one")

    def _decide(self, want, candidates, track, store_id: str):
        """Match, and record the decision whether it succeeded or not.

        A refusal is written down for the same reason a success is: the interface
        shows the user which candidate was turned down and by how much, and that
        is only possible if the losing decision was kept.
        """
        if self._user_confirmed(track["id"]):
            decision, index = match.best_match(want, candidates)
            score = getattr(decision, "score", 0) or 0
            if index is not None and score >= MATCH_CONFIRM_MIN:
                self._record(track["id"], want, candidates[index], score,
                             "accepted_after_confirmation",
                             match.explain(want, candidates[index], decision),
                             len(candidates), store_id,
                             gate_failed=decision.gate_failed, matched_via=decision.matched_via)
                return decision, index
            # Confirmation is permission to accept a near miss, not permission to
            # download anything at all. Below the confirm floor there is no
            # candidate worth attaching someone's intent to, so this still refuses.
            self._record(track["id"], want, candidates[index] if index is not None else None,
                         score, "refused_despite_confirmation",
                         "confirmed by the user, but no candidate reaches the confirm floor",
                         len(candidates), store_id,
                         gate_failed=decision.gate_failed, matched_via=decision.matched_via)
            raise MatchRefused(
                f"You confirmed this, but nothing in {store_id} comes close enough to be "
                f"that track (best score {score}).", score=score, reason="below_confirm_floor")

        try:
            decision, index = match.require_match(want, candidates)
        except MatchRefused as refusal:
            # `str(refusal)` is `reader_message(...)`, the same sentence
            # `require_match` raised, so the refusal panel and the exception a
            # caller might log agree word for word. The short gate code that
            # `reasons` used to carry instead now has its own column, so
            # neither the human sentence nor the machine-readable gate is lost.
            self._record(track["id"], want, getattr(refusal, "candidate", None),
                         getattr(refusal, "score", None), "refused", str(refusal),
                         len(candidates), store_id,
                         gate_failed=getattr(refusal, "reason", "") or None)
            raise

        # The confirm band exists to be confirmed. Reaching auto requires a
        # shared MBID or ISRC, so anything decided on strings alone stops here
        # and asks, which is what makes "strings alone never auto-claim" true of
        # the running program and not only of the design.
        if getattr(decision, "outcome", "") == "confirm":
            chosen = candidates[index] if index is not None else None
            message = (match.reader_message(want, chosen, decision) if chosen
                      else "Nothing in the account came close enough to be this track.")
            self._record(track["id"], want, chosen, decision.score, "needs_confirmation",
                         message, len(candidates), store_id,
                         gate_failed=decision.gate_failed, matched_via=decision.matched_via)
            raise MatchRefused(message, score=decision.score, candidate=chosen,
                               reason="needs_confirmation")

        chosen = candidates[index] if index is not None else None
        self._record(track["id"], want, chosen, getattr(decision, "score", None), "accepted",
                     match.explain(want, chosen, decision) if chosen else "",
                     len(candidates), store_id,
                     gate_failed=decision.gate_failed, matched_via=decision.matched_via)
        return decision, index

    def _user_confirmed(self, track_id: int) -> bool:
        """Whether the newest decision for this track is a user confirmation.

        Newest rather than any, so a confirmation is spent once. Otherwise a
        single override would silently license every future claim on that track,
        including ones about a different candidate the user never saw.
        """
        conn = self.svc.db()
        try:
            row = conn.execute(
                "SELECT outcome FROM match_decision WHERE track_id=?"
                " ORDER BY decided_at DESC, id DESC LIMIT 1", (track_id,)).fetchone()
        finally:
            conn.close()
        # `swept` is the same licence arrived at differently: a sweep matched
        # this track against a purchase in the confirm band without anyone
        # watching. Kept as its own outcome rather than written as a user
        # confirmation, so the audit never claims a person looked at something
        # they did not. Spent once, exactly as a confirmation is.
        return bool(row) and row["outcome"] in ("user_confirmed", "swept")

    def _user_picked_item(self, track_id: int) -> str | None:
        """The item_key of a hand-picked purchase, or None.

        Read off the newest decision for the track, same as `_user_confirmed`
        and for the same reason: a pick is spent once, so a track refused again
        later does not silently keep downloading whatever was picked before.
        The identity itself travels in `candidate_json` rather than a new
        column, because that is where a decision's winner already lives; a
        pick has no scored candidate, but it has exactly this.
        """
        conn = self.svc.db()
        try:
            row = conn.execute(
                "SELECT outcome, candidate_json FROM match_decision WHERE track_id=?"
                " ORDER BY decided_at DESC, id DESC LIMIT 1", (track_id,)).fetchone()
        finally:
            conn.close()
        if not row or row["outcome"] != "user_picked":
            return None
        try:
            payload = json.loads(row["candidate_json"] or "{}")
        except ValueError:
            return None
        return payload.get("item_key") or None

    def _record(self, track_id: int, want, candidate, score, outcome: str,
                reasons: str, considered: int, store_id: str | None, *,
                gate_failed: str | None = None, matched_via: str = "") -> None:
        """Write the audit row for one matching decision.

        `reasons` is prose now, not a code: the sentence the refusal panel
        shows next to "Confidence {score} / {threshold}". `gate_failed` and
        `matched_via` are what used to be folded into that same string for a
        gate failure; they get their own columns so a refusal reads as a
        sentence a person can act on without losing the machine-readable gate
        an audit needs.

        Failure here is logged at error level rather than swallowed quietly. An
        audit that stops recording without saying so is worse than none, because
        the refusal screen would go blank and read as "nothing happened" rather
        than "the record is missing".
        """
        conn = self.svc.db()
        try:
            conn.execute(
                "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                " matcher_version, lexicon_hash, outcome, score, gate_failed, matched_via,"
                " reasons, query_json, candidate_json, candidates_considered, chosen_store_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (track_id, int(time.time()), "claim", store_id,
                 getattr(match, "MATCHER_VERSION", "1"), identity.lexicon_hash(),
                 outcome, score, gate_failed, matched_via, reasons[:2000],
                 json.dumps(_describe(want), default=str),
                 json.dumps(_describe(candidate), default=str) if candidate else None,
                 considered, store_id),
            )
        except Exception as exc:
            log.error("match decision not recorded: %s: %s", type(exc).__name__, exc,
                      context={"track": track_id, "outcome": outcome})
        finally:
            conn.close()


def register(svc: Any, stores: dict[str, Any]) -> None:
    """Wire the pipeline into the job queue."""
    svc.jobs.register("claim", ClaimPipeline(svc, stores))
