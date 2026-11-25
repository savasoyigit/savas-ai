import pytest

import model_router
from model_router import ACCESS, FATAL, LIMIT, TRANSIENT, CircuitBreaker, classify_failure, parse_retry_after


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def breaker(clock):
    return CircuitBreaker(clock=clock)


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("status, body, kind", [
    (None, "", TRANSIENT),            # timeout / connection error
    (466, "", TRANSIENT),
    (500, "", TRANSIENT),
    (503, "", TRANSIENT),
    (429, "", LIMIT),
    (403, "Monthly quota exceeded", LIMIT),
    (402, "You hit the rate limit", LIMIT),
    (500, "upstream: rate_limit_exceeded", LIMIT),
    (400, "The requested model is not supported.", FATAL),
    (400, "quota", FATAL),            # bad request stays a config error
    (401, "rate limit", FATAL),
    (401, "", FATAL),
    (403, "forbidden", ACCESS),      # permission problem: fallback + alert
    (403, "", ACCESS),
    (404, "", FATAL),
])
def test_classify_failure(status, body, kind):
    assert classify_failure(status, body) == kind


@pytest.mark.parametrize("value, expected", [
    ("30", 30.0),
    (" 120 ", 120.0),
    (None, None),
    ("", None),
    ("soon", None),
    ("0", None),
    ("-5", None),
    ("Fri, 13 Nov 2026 12:00:40 GMT", 40.0),
    ("Fri, 13 Nov 2026 11:00:00 GMT", None),   # already past
])
def test_parse_retry_after(value, expected):
    now = 1794571200.0  # 2026-11-13 12:00:00 UTC
    assert parse_retry_after(value, now=now) == expected


# --- breaker --------------------------------------------------------------

def test_two_consecutive_transient_failures_open_for_ten_minutes(breaker, clock):
    assert breaker.record_failure("m", TRANSIENT) is None
    assert breaker.unavailable() == frozenset()
    assert breaker.record_failure("m", TRANSIENT) == clock.now + 600
    assert breaker.unavailable() == {"m"}

    clock.now += 599
    assert breaker.unavailable() == {"m"}
    clock.now += 1
    assert breaker.unavailable() == frozenset()


def test_success_resets_the_counter(breaker):
    breaker.record_failure("m", TRANSIENT)
    breaker.record_success("m")
    assert breaker.record_failure("m", TRANSIENT) is None
    assert breaker.unavailable() == frozenset()


def test_counter_starts_again_after_cooldown(breaker, clock):
    breaker.record_failure("m", TRANSIENT)
    breaker.record_failure("m", TRANSIENT)
    clock.now += 600
    assert breaker.expire() == ["m"]
    assert breaker.record_failure("m", TRANSIENT) is None
    assert breaker.unavailable() == frozenset()


def test_limit_failure_opens_on_first_error(breaker, clock):
    assert breaker.record_failure("m", LIMIT) == clock.now + 600
    assert breaker.unavailable() == {"m"}


def test_limit_failure_honours_retry_after(breaker, clock):
    assert breaker.record_failure("m", LIMIT, retry_after=30) == clock.now + 30
    clock.now += 30
    assert breaker.unavailable() == frozenset()


def test_retry_after_is_capped(breaker, clock):
    opened = breaker.record_failure("m", LIMIT, retry_after=10**9)
    assert opened == clock.now + model_router.MAX_COOLDOWN_SECONDS


def test_fatal_failure_is_not_counted(breaker):
    for _ in range(5):
        assert breaker.record_failure("m", FATAL) is None
    assert breaker.unavailable() == frozenset()


def test_access_failures_count_like_transient_ones(breaker, clock):
    assert breaker.record_failure("m", ACCESS) is None
    assert breaker.record_failure("m", ACCESS) == clock.now + 600


def test_only_the_failing_model_is_closed(breaker):
    breaker.record_failure("gh/a", LIMIT)
    assert breaker.unavailable() == {"gh/a"}
    assert breaker.record_failure("gh/b", TRANSIENT) is None


def test_already_open_model_is_not_reported_again(breaker, clock):
    first = breaker.record_failure("m", LIMIT, retry_after=30)
    # last-resort calls can still hit an open model; a longer wait extends it
    assert breaker.record_failure("m", LIMIT, retry_after=90) is None
    assert first == clock.now + 30
    clock.now += 60
    assert breaker.unavailable() == {"m"}


def test_success_closes_an_open_model(breaker):
    breaker.record_failure("m", LIMIT)
    assert breaker.record_success("m") is True
    assert breaker.unavailable() == frozenset()
    assert breaker.record_success("m") is False


def test_expire_reports_each_model_once(breaker, clock):
    breaker.record_failure("b", LIMIT, retry_after=10)
    breaker.record_failure("a", LIMIT, retry_after=20)
    clock.now += 15
    assert breaker.expire() == ["b"]
    assert breaker.expire() == []
    clock.now += 10
    assert breaker.expire() == ["a"]
