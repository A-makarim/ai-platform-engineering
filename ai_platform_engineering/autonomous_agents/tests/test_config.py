# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``Settings`` validation, focused on the IMP-02 fields.

These exist so a future refactor that loosens or removes the bounds on the
A2A retry/timeout settings shows up as a failing test rather than as a
silent regression that lets ``A2A_TIMEOUT_SECONDS=-5`` slip through.
"""

import pydantic
import pytest

from autonomous_agents.config import Settings


def test_a2a_settings_have_sensible_defaults():
    s = Settings()
    assert s.a2a_timeout_seconds == 300.0
    assert s.a2a_max_retries == 3
    assert s.a2a_retry_backoff_initial_seconds == 1.0
    assert s.a2a_retry_backoff_max_seconds == 30.0


def test_a2a_timeout_must_be_positive():
    for bad in (0, -1, -0.5):
        with pytest.raises(pydantic.ValidationError):
            Settings(a2a_timeout_seconds=bad)


def test_a2a_max_retries_must_be_non_negative():
    with pytest.raises(pydantic.ValidationError):
        Settings(a2a_max_retries=-1)
    # 0 is the meaningful "no retries" value and must be accepted
    assert Settings(a2a_max_retries=0).a2a_max_retries == 0


def test_a2a_backoff_max_must_be_positive():
    for bad in (0, -1):
        with pytest.raises(pydantic.ValidationError):
            Settings(a2a_retry_backoff_max_seconds=bad)


def test_a2a_settings_reject_inf_and_nan():
    """``float("inf")`` and ``float("nan")`` would silently break httpx
    timeouts and tenacity wait calculations respectively. The Settings
    validator catches both at construction time.
    """
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(pydantic.ValidationError):
            Settings(a2a_timeout_seconds=bad)
        with pytest.raises(pydantic.ValidationError):
            Settings(a2a_retry_backoff_max_seconds=bad)


# --- IMP-05: CORS safety -------------------------------------------------


def test_cors_origins_default_is_empty():
    """No origins by default. The dev launcher script supplies localhost
    explicitly via env -- production must opt in by listing origins."""
    assert Settings().cors_origins == []


def test_cors_origins_accepts_explicit_list():
    s = Settings(cors_origins=["http://localhost:3000", "https://app.example.com"])
    assert s.cors_origins == ["http://localhost:3000", "https://app.example.com"]


def test_cors_origins_parses_comma_separated_string():
    """Operators commonly paste a comma-list into ``.env`` rather than
    JSON. The pre-validator splits on commas so the obvious
    ``CORS_ORIGINS=http://a,http://b`` form Just Works instead of
    crashing with a JSON parse error."""
    s = Settings(cors_origins="http://localhost:3000, https://app.example.com")
    assert s.cors_origins == ["http://localhost:3000", "https://app.example.com"]


def test_cors_origins_rejects_wildcard_alone():
    """``*`` plus ``allow_credentials=True`` (the FastAPI default) is a
    spec violation -- modern browsers refuse the response. Failing
    fast at startup beats discovering the misconfig from a debug
    session months later."""
    with pytest.raises(pydantic.ValidationError):
        Settings(cors_origins=["*"])


def test_cors_origins_rejects_wildcard_in_mixed_list():
    """Even one ``*`` in an otherwise-explicit list is unsafe -- the
    whole list short-circuits to "any origin"."""
    with pytest.raises(pydantic.ValidationError):
        Settings(cors_origins=["http://localhost:3000", "*"])
