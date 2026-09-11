from app.ratelimit import RateLimiter


def test_legacy_rate_limiter_remains_available_for_disabled_controls() -> None:
    limiter = RateLimiter(1, 0)
    assert limiter.allow("session") is True
    assert limiter.allow("session") is False
