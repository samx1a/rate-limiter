from fastapi.testclient import TestClient

from webapp.main import create_app
from webapp.settings import Settings
from webapp.simulate import Scenario, max_in_span, simulate


def rows(scenario: Scenario) -> dict[str, dict[str, object]]:
    return {str(r["algorithm"]): r for r in simulate(scenario)["rows"]}  # type: ignore[attr-defined]


def test_edge_burst_shows_fixed_window_letting_twice_the_limit_through() -> None:
    result = rows(Scenario.EDGE_BURST)
    assert result["fixed_window"]["max_in_burst_span"] == 10  # 2 x limit within 2s
    assert result["sliding_window"]["max_in_burst_span"] == 5
    assert result["sliding_counter"]["max_in_burst_span"] == 5
    # At t=15 half the earlier window is still in view: 2.5 counted, so 2 more fit.
    assert result["sliding_counter"]["admitted"] == 7
    # Token bucket spends its burst of 5 before the edge; 0.5/s refill admits nothing
    # more within the next second.
    assert result["token_bucket"]["max_in_burst_span"] == 5
    assert result["token_bucket"]["admitted"] == 8  # 3 more once tokens refill at t=15


def test_steady_overload_is_capped_by_every_algorithm() -> None:
    for name, row in rows(Scenario.STEADY).items():
        assert 0 < row["admitted"] <= 20, name  # type: ignore[operator]


def test_max_in_span() -> None:
    assert max_in_span([0, 1, 2, 10, 11], 10) == 3
    assert max_in_span([0, 1, 2, 10, 11], 2) == 2
    assert max_in_span([], 10) == 0


def test_demo_page_and_simulate_route() -> None:
    c = TestClient(create_app(Settings()))
    page = c.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "<title>" in page.text
    data = c.get("/api/simulate", params={"scenario": "steady"}).json()
    assert data["scenario"] == "steady" and len(data["rows"]) == 4
    assert c.get("/api/simulate", params={"scenario": "nope"}).status_code == 422
