import httpx2 as httpx
import pytest

from examples.client import get_with_retries, retry_delay


def respond(*responses: httpx.Response | Exception) -> httpx.Client:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler))


def limited(seconds: str) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": seconds})


def test_waits_retry_after_plus_jitter_never_less() -> None:
    assert retry_delay(limited("3"), 0, rand=lambda: 0.0) == 3.0
    assert retry_delay(limited("3"), 0, rand=lambda: 1.0) == pytest.approx(3.3)


def test_backs_off_exponentially_with_full_jitter_without_a_header() -> None:
    assert retry_delay(None, 0, rand=lambda: 1.0) == 0.5
    assert retry_delay(None, 3, rand=lambda: 1.0) == 4.0
    assert retry_delay(None, 20, rand=lambda: 1.0) == 30.0  # capped
    assert retry_delay(None, 3, rand=lambda: 0.25) == 1.0


def test_unparseable_retry_after_falls_back_to_backoff() -> None:
    date = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert retry_delay(date, 1, rand=lambda: 1.0) == 1.0


def test_retries_a_429_after_waiting_then_succeeds() -> None:
    slept: list[float] = []
    client = respond(limited("2"), httpx.Response(200))
    response = get_with_retries(client, "http://x/", sleep=slept.append, rand=lambda: 0.0)
    assert response.status_code == 200
    assert slept == [2.0]


def test_gives_up_after_the_last_attempt_and_returns_the_429() -> None:
    slept: list[float] = []
    client = respond(*(limited("1") for _ in range(3)))
    response = get_with_retries(client, "http://x/", attempts=3, sleep=slept.append)
    assert response.status_code == 429
    assert len(slept) == 2  # no pointless sleep after the final attempt


def test_network_errors_are_retried_then_raised() -> None:
    slept: list[float] = []
    error = httpx.ConnectError("refused")
    ok = respond(error, httpx.Response(200))
    assert get_with_retries(ok, "http://x/", sleep=slept.append).status_code == 200
    bad = respond(error, error)
    with pytest.raises(httpx.ConnectError):
        get_with_retries(bad, "http://x/", attempts=2, sleep=slept.append)


def test_other_errors_are_not_retried() -> None:
    slept: list[float] = []
    client = respond(httpx.Response(400))
    assert get_with_retries(client, "http://x/", sleep=slept.append).status_code == 400
    assert slept == []
