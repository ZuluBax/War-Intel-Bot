"""Log redaction: API keys must never reach the logs, even at DEBUG."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import structlog

from app.config.settings import Settings
from app.utils.logging import _redact_processor, configure_logging


def _redact(**fields: object) -> dict[str, object]:
    return dict(_redact_processor(None, "info", dict(fields)))


def test_key_in_a_url_query_string_is_masked() -> None:
    # The exact shape httpx logs at DEBUG for an FFScouter call.
    out = _redact(
        event='HTTP Request: GET https://ffscouter.com/api/v1/get-stats?key=ffsc0123456789ab&targets=1,2 "HTTP/1.1 200 OK"'
    )
    assert "ffsc0123456789ab" not in str(out["event"])
    assert "key=***REDACTED***" in str(out["event"])
    assert "targets=1,2" in str(out["event"])  # the rest of the URL survives


@pytest.mark.parametrize("param", ["key", "api_key", "apikey", "token"])
def test_each_secret_query_param_name_is_masked(param: str) -> None:
    out = _redact(detail=f"GET https://example.test/x?a=1&{param}=s3cretvalue&b=2")
    assert "s3cretvalue" not in str(out["detail"])
    assert "a=1" in str(out["detail"])
    assert "b=2" in str(out["detail"])


def test_a_param_merely_ending_in_key_is_left_alone() -> None:
    # "monkey=" is not a secret; only whole parameter names are matched.
    out = _redact(detail="GET https://example.test/x?monkey=banana")
    assert out["detail"] == "GET https://example.test/x?monkey=banana"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved = {n: logging.getLogger(n).level for n in ("httpx", "httpcore", "discord")}
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


@pytest.mark.usefixtures("restore_logging")
def test_debug_logging_still_keeps_httpx_quiet(settings: Settings) -> None:
    # httpx logs every request URL at INFO/DEBUG, and FFScouter's URL carries the key.
    configure_logging(settings.model_copy(update={"log_level": "DEBUG"}))
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
