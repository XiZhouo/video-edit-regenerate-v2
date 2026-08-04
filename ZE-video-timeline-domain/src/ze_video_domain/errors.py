"""Domain error types — framework-agnostic.

The domain layer never imports the build-v3 sdk, so it raises its own errors.
Each hosting skill's stage layer catches these and maps them onto ``sdk.ZeError``
(e.g. ``RequiresRuntime`` → ``ZeError("requires_runtime", …)``). The split of
"transport/config problem (retryable)" vs "gateway said no (failed)" is decided
by the caller/stage, mirroring gen-timeline's client-raises / stage-classifies
discipline.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base for all video-domain errors."""

    code = "domain_error"

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}


class ValidationFailed(DomainError):
    code = "validation_failed"


class RequiresRuntime(DomainError):
    """A local binary or external service needed to complete is unavailable.

    Not a failure of the request — the runtime is missing/unreachable. The stage
    layer surfaces this as ``requires_runtime`` so the UI can prompt the user to
    provision the tool instead of marking the work failed.
    """

    code = "requires_runtime"
