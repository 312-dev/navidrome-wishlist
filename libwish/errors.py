"""The single error hierarchy.

Providers are only ever allowed to raise `ProviderError` and its subclasses. The
runtime catches everything else, logs it with a traceback, and reports it as
`TransientError(code="unexpected")` so that one badly behaved provider degrades
into a retry rather than taking a poll cycle or a claim down with it.

The split that matters to callers is retry policy, so it is encoded in the type
rather than left to the caller to infer from a message: `TransientError` means
try again unchanged, `PermanentError` means do not, and `AuthExpired` means stop
and get a human to reconnect the account.
"""


class LibwishError(Exception):
    """Root of everything this application raises deliberately."""


class ConfigError(LibwishError):
    """Configuration is missing or contradictory. Fatal at startup.

    Raised before any work begins, so the message is read by someone at a
    terminal and should name the environment variable at fault.
    """


class ProviderError(LibwishError):
    """Base for every failure a source or store provider is permitted to raise.

    `code` is a short stable slug for the failure mode, suitable for grouping in
    `/api/status` and for tests to assert on. Human-facing text goes in the
    message; `code` should stay parseable and unchanging.
    """

    def __init__(self, message: str = "", *, code: str = "", provider_id: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.provider_id = provider_id

    def __str__(self) -> str:
        bits = [b for b in (self.provider_id, self.code) if b]
        return f"[{' '.join(bits)}] {self.message}" if bits else self.message


class TransientError(ProviderError):
    """The same call, unchanged, is worth making again later."""


class Unreachable(TransientError):
    """The remote host could not be contacted at all: DNS, connect, or timeout."""


class RateLimited(TransientError):
    """The remote asked us to slow down.

    `retry_after` is seconds, taken from the response when the server states one.
    None means the server refused without saying for how long, and the caller
    applies its own backoff.
    """

    def __init__(self, message: str = "", *, retry_after: float | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class AuthExpired(ProviderError):
    """The stored credential no longer works and only a human can fix it.

    A single 403 is not sufficient grounds to raise this. Some stores answer 403
    for a live session that merely lacks rights to one item, and treating that as
    an expired login logs the user out of a working account. The auth layer
    re-probes a known-good endpoint first and raises this only if that probe also
    fails.
    """


class PermanentError(ProviderError):
    """Retrying will not change the outcome."""


class NotFound(PermanentError):
    """The remote is working and says the thing does not exist."""


class NotSupported(PermanentError):
    """The provider does not implement this capability.

    Declared capabilities are meant to keep the runtime from ever calling into
    this, so reaching it is a wiring bug rather than a user-visible condition.
    """


class MatchRefused(LibwishError):
    """Identity could not be established well enough to spend money or write a file.

    Refusal is a successful outcome of matching, not an error in the sense of
    something having gone wrong, but it has to interrupt the claim, so it travels
    as an exception. It carries the rejected candidate because the interface shows
    the user exactly what was turned down and why, and a refusal the user cannot
    see is indistinguishable from the application doing nothing.
    """

    def __init__(self, message: str = "", *, score: int | None = None,
                 candidate: object = None, reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.score = score
        self.candidate = candidate
        self.reason = reason


class StoreAuthError(AuthExpired):
    """A store's session is dead and reconnecting it needs a person.

    Distinct from returning an empty list of owned items. A store that answers
    "you own nothing" because it does not recognise the session would otherwise
    look identical to one that genuinely holds no purchases, and the pipeline
    would conclude the user does not own a track they paid for.
    """


class StorePreparing(TransientError):
    """The store is still encoding the file and asks us to come back."""


class StoreFormatUnavailable(PermanentError):
    """None of the requested formats exists for this item."""


class VerificationFailed(LibwishError):
    """A downloaded file is not what the store said it was.

    Raised after a download completes but before the file is published into the
    library, so the library never gains a file that failed its own check.
    """
