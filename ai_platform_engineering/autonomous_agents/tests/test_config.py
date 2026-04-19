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
