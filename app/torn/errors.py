"""Torn API error types, mapped from Torn's own numeric error codes.

Verified against https://www.torn.com/api.html and the v2 playground
(see docs/api-contracts.md). Every error message has the API key stripped —
errors are constructed from Torn's JSON body, never from raw httpx exception
text that could echo the Authorization header.
"""

from __future__ import annotations


class TornAPIError(Exception):
    """Base class for all Torn API errors. Never contains the API key."""


class TornAPIAuthError(TornAPIError):
    """Codes 1, 2, 10, 13, 18: key missing / invalid / disabled / paused / owner in fedjail."""


class TornAPIRateLimitError(TornAPIError):
    """Code 5: too many requests (100/min per user exceeded)."""


class TornAPIIPBlockError(TornAPIError):
    """Code 8: temporary IP ban from key abuse. Back off hard; do not hammer."""


class TornAPIAccessLevelError(TornAPIError):
    """Code 16: the key's access level is too low for this selection."""


class TornAPITransientError(TornAPIError):
    """Codes 9, 17, 24: backend down / temporary error / API disabled — safe to retry."""


class TornAPIRequestError(TornAPIError):
    """Any other documented Torn error code — carries Torn's own message."""


_ERROR_CODE_MAP: dict[int, type[TornAPIError]] = {
    1: TornAPIAuthError,
    2: TornAPIAuthError,
    5: TornAPIRateLimitError,
    8: TornAPIIPBlockError,
    9: TornAPITransientError,
    10: TornAPIAuthError,
    13: TornAPIAuthError,
    16: TornAPIAccessLevelError,
    17: TornAPITransientError,
    18: TornAPIAuthError,
    24: TornAPITransientError,
}

RETRYABLE: tuple[type[TornAPIError], ...] = (TornAPITransientError,)


def error_for_code(code: int, message: str) -> TornAPIError:
    error_type = _ERROR_CODE_MAP.get(code, TornAPIRequestError)
    return error_type(f"Torn API error {code}: {message}")
