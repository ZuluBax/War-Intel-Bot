"""FFScouter API error types.

Mapped from FFScouter's numeric error codes (see docs/api-contracts.md).
Messages never contain the API key.
"""

from __future__ import annotations


class FFScouterError(Exception):
    """Base class for all FFScouter API errors. Never contains the API key."""


class FFScouterAuthError(FFScouterError):
    """Code 6 (HTTP 401): invalid or unregistered API key."""


class FFScouterRateLimitError(FFScouterError):
    """Code 20 (HTTP 429): 20 requests/minute per IP exceeded. Back off and retry."""


class FFScouterPremiumRequiredError(FFScouterError):
    """Code 19 (HTTP 403): the requested data needs an FFScouter premium key."""


class FFScouterRequestError(FFScouterError):
    """Any other FFScouter error code — carries FFScouter's own message."""


RETRYABLE: tuple[type[FFScouterError], ...] = (FFScouterRateLimitError,)


def error_for_code(code: int, message: str, *, http_status: int | None = None) -> FFScouterError:
    mapping: dict[int, type[FFScouterError]] = {
        6: FFScouterAuthError,
        19: FFScouterPremiumRequiredError,
        20: FFScouterRateLimitError,  # per-account budget (premium endpoints)
        21: FFScouterRateLimitError,  # per-IP / per-route budget
    }
    error_type = mapping.get(code, FFScouterRequestError)
    suffix = f" (HTTP {http_status})" if http_status is not None else ""
    return error_type(f"FFScouter error {code}: {message}{suffix}")
