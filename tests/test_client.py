"""Retry behaviour for the live transport."""

import pytest

from autowriter.gdocs import client as api


class _Response:
    def __init__(self, status):
        self.status = status


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__("HTTP %d" % status)
        self.resp = _Response(status)


def _failing(statuses, result="ok"):
    """A call that raises the given statuses in turn, then succeeds."""
    remaining = list(statuses)

    def call():
        if remaining:
            raise _HttpError(remaining.pop(0))
        return result

    return call


def test_transient_failures_are_retried():
    waits = []
    assert api._with_retries(_failing([503, 500]), sleep=waits.append) == "ok"
    assert len(waits) == 2


def test_a_rate_limit_waits_out_the_quota_window():
    # The write quota is per minute, so a one-second backoff would exhaust every
    # attempt inside the same window and fail a copy that only needed to wait.
    waits = []
    assert api._with_retries(_failing([429]), sleep=waits.append) == "ok"
    assert waits[0] >= api.RATE_LIMIT_DELAY


def test_rate_limit_backoff_grows_but_stays_bounded():
    waits = []
    api._with_retries(_failing([429, 429, 429]), sleep=waits.append)
    assert waits[1] > waits[0]
    assert max(waits) <= api.MAX_RATE_LIMIT_DELAY + 1


def test_a_permanent_failure_is_not_retried():
    waits = []
    with pytest.raises(_HttpError):
        api._with_retries(_failing([400]), sleep=waits.append)
    assert waits == []


def test_retries_give_up_and_raise_the_last_error():
    waits = []
    with pytest.raises(_HttpError):
        api._with_retries(_failing([503] * api.MAX_ATTEMPTS), sleep=waits.append)
    assert len(waits) == api.MAX_ATTEMPTS - 1
