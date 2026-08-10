"""Sweep every shop's purchase history and claim what it recognises.

A claim answers "I bought this track, go and find it". This answers the other
direction: "here is everything I own at this shop, which of it is on my list".
That inversion is the whole point. A claim enumerates a shop once per track, so
buying five things meant five identical trips; this enumerates once per shop no
matter how many rows come back.

What it does NOT do is decide anything new. A purchase is only acted on when it
clears the same gate a claim clears, and everything it acts on becomes an
ordinary claim job, so the download, the verification and the audit row are the
ones that already existed. A sweep that filed things by its own standard would
be a second, quieter matcher, and the whole posture of this application is that
there is one and it refuses when unsure.

Below the gate nothing is recorded against the track. A purchase that nearly
matched is reported in the job's own result and left alone: marking a track
owned on a guess removes it from the list, and the reader never learns it was a
guess, which is the one failure here worth designing against.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import identity, match
from .errors import LibwishError
from .log import get
from .models import MATCH_CONFIRM_MIN

log = get("sync")

# One shop that is down must not sink the sweep: the others still have
# purchases worth finding, and "Bandcamp is signed out" is a thing to report
# rather than a reason to do nothing.
class ShopResult:
    __slots__ = ("store", "items", "problem")

    def __init__(self, store, items=None, problem=""):
        self.store = store
        self.items = items or []
        self.problem = problem


class SyncPipeline:
    """Runs one sweep. Constructed per job so it can carry per-run state."""

    def __init__(self, svc: Any, stores: dict[str, Any]) -> None:
        self.svc = svc
        self.stores = stores

    def __call__(self, job, progress) -> None:
        stores = list(self.stores.values())
        if not stores:
            raise LibwishError("No shops are configured, so there is nothing to sweep.")

        progress("session", shops=len(stores))
        results = self._enumerate(stores, progress)

        reachable = [r for r in results if not r.problem]
        if not reachable:
            raise LibwishError(
                "No shop could be read: "
                + "; ".join(f"{r.store.name}: {r.problem}" for r in results)
            )

        progress("match", purchases=sum(len(r.items) for r in reachable))
        queued, near = self._match(reachable)

        # Reported on the job rather than logged and forgotten. A sweep that
        # finished having queued nothing has to be able to say whether that is
        # because everything was already filed or because nothing matched.
        progress("queue",
                 queued=len(queued),
                 near_misses=len(near),
                 shops_read=len(reachable),
                 shops_skipped=[{"shop": r.store.name, "why": r.problem}
                                for r in results if r.problem],
                 matched=[{"track_id": t, "shop": s, "title": ti} for t, s, ti in queued],
                 near=near)
        log.info("sweep finished", context={"queued": len(queued), "near": len(near)})

    def _enumerate(self, stores, progress) -> list[ShopResult]:
        """Read every shop's purchases, in parallel.

        Concurrent because these are independent network round trips against
        different hosts, and a sweep of three shops should take as long as the
        slowest one rather than the sum. Each shop's failure is captured as its
        own result rather than raised, so one dead session does not discard the
        purchases already read from the others.
        """
        def read(store) -> ShopResult:
            try:
                health = store.check()
                if not health.ok:
                    return ShopResult(store, problem=f"unreachable ({health.detail})")
                if not health.authed:
                    return ShopResult(store, problem="signed out")
                return ShopResult(store, items=list(store.list_owned()))
            except Exception as exc:  # noqa: BLE001 - one shop must not sink the sweep
                log.warning("could not read a shop", context={"shop": store.id, "error": str(exc)})
                return ShopResult(store, problem=str(exc))

        with ThreadPoolExecutor(max_workers=max(1, len(stores))) as pool:
            results = list(pool.map(read, stores))
        progress("enumerate",
                 shops=len(results),
                 purchases=sum(len(r.items) for r in results))
        return results

    def _match(self, results: list[ShopResult]):
        """Pair unfiled purchases with wanted tracks, above the confirm band.

        The matcher is asked in its own direction: a wanted track is the query
        and the purchases are the candidates, exactly as a single claim asks
        it. It is not symmetric, and asking it the other way round scores an
        obvious pair at zero, because the version and edition handling expects
        the release-shaped side to be the candidate. That was this function's
        first bug: "Gold Dust Woman" against "Gold Dust Woman (2004 Remaster)"
        came back as no relationship at all.

        The efficiency this exists for is untouched by that. Every shop is
        still read once; what changed is only which side of the comparison the
        loop walks.
        """
        wanted = self.svc.tracks.queued()
        if not wanted:
            return [], []

        # Every unfiled purchase, across every shop, as one candidate list.
        pool: list[tuple[Any, Any]] = []
        for result in results:
            filed = self.svc.tracks.owned_item_keys(result.store.id)
            for item in result.items:
                # Already filed against something. This is what makes a sweep
                # cheap to re-run and safe to press twice.
                if item.item_key not in filed:
                    pool.append((result.store, item))
        if not pool:
            return [], []

        candidates = [identity.from_owned_item(item) for _, item in pool]
        queued: list[tuple[int, str, str]] = []
        near: list[dict] = []
        # A purchase can only be spent once. Two tracks that resemble the same
        # recording would otherwise queue two claims for one file.
        spent: set[int] = set()

        for track in wanted:
            want = identity.build_identity(track["artist"], track["title"])
            decision, index = match.best_match(want, candidates)
            if index is None or index in spent:
                continue

            store, item = pool[index]
            score = getattr(decision, "score", 0) or 0

            if decision.outcome == "refused" or score < MATCH_CONFIRM_MIN:
                # A zero means nothing in the account resembled this track at
                # all, which is the ordinary case for a list of 159 wants and
                # a handful of purchases. Reporting that as "too close to call"
                # was this function's second bug: it turned "you have not
                # bought this" into a near miss against whichever row happened
                # to sort first.
                if score > 0:
                    near.append({
                        "shop": store.name,
                        "purchase": item.title,
                        "closest": f'{track["artist"]} - {track["title"]}',
                        "score": score,
                        "needs": MATCH_CONFIRM_MIN,
                    })
                continue

            spent.add(index)
            # Recorded before the claim is queued, because the claim will
            # re-derive this same match and refuse it: the confirm band is
            # exactly the band that asks a human first. This row is that
            # answer, given by the sweep rather than by a person, which is
            # why it is `swept` and not `user_confirmed`.
            self._record_sweep(track["id"], store.id, item, score)
            self.svc.jobs.enqueue("claim", track_id=track["id"], provider_id=store.id)
            queued.append((track["id"], store.name, item.title))

        return queued, near

    def _record_sweep(self, track_id: int, store_id: str, item, score) -> None:
        """Write the audit row that licenses the claim this sweep queues."""
        import json
        import time

        from . import identity, match as matchmod

        track = self.svc.tracks.get(track_id)
        conn = self.svc.db()
        try:
            conn.execute(
                "INSERT INTO match_decision(track_id, decided_at, phase, provider,"
                " matcher_version, lexicon_hash, outcome, reasons, query_json,"
                " candidate_json, candidates_considered, chosen_store_id)"
                " VALUES(?,?,?,?,?,?,'swept',?,?,?,0,?)",
                (track_id, int(time.time()), "sync", store_id,
                 getattr(matchmod, "MATCHER_VERSION", "1"), identity.lexicon_hash(),
                 f"matched by a purchase sweep at {score}, nobody was asked",
                 json.dumps({"artist": track["artist"], "title": track["title"]}),
                 json.dumps({"item_key": item.item_key, "title": item.title,
                             "artist": item.artist}),
                 store_id),
            )
        finally:
            conn.close()


def register(svc: Any, stores: dict[str, Any]) -> None:
    """Wire the pipeline into the job queue."""
    svc.jobs.register("sync", SyncPipeline(svc, stores))
