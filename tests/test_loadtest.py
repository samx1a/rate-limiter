from loadtest.loadtest import max_admissible, percentile
from ratelimit.limits import Algorithm, LimitSpec


def test_max_admissible_bounds() -> None:
    spec = LimitSpec(10, 10)
    assert max_admissible(Algorithm.TOKEN_BUCKET, spec, 20) == 30  # burst 10 + 1/s * 20s
    assert max_admissible(Algorithm.SLIDING_WINDOW, spec, 20) == 20
    assert max_admissible(Algorithm.SLIDING_WINDOW, spec, 21) == 30
    assert max_admissible(Algorithm.FIXED_WINDOW, spec, 20) == 30


def test_percentile() -> None:
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile([], 50) == 0


def run_against(app_settings: object, argv: list[str]) -> dict[str, object]:
    """Run the real load test tool against a real server on a free port."""
    import asyncio
    import json
    import socket
    import threading
    import time

    import uvicorn

    from loadtest import loadtest
    from webapp.main import create_app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(app_settings), port=port, log_level="warning")  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        args = loadtest.build_parser().parse_args(
            ["--url", f"http://127.0.0.1:{port}/api/hello", *argv]
        )
        result = asyncio.run(loadtest.run(args))
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    return json.loads(json.dumps(loadtest.asdict(result)))


def test_open_loop_sends_on_schedule_and_never_over_admits() -> None:
    from webapp.settings import Settings

    settings = Settings(user_limit=LimitSpec(5, 10), ip_limit=LimitSpec(10_000, 1))
    result = run_against(
        settings,
        [
            "--rate",
            "100",
            "--duration",
            "1",
            "--users",
            "2",
            "--limit",
            "5/10s",
            "--concurrency",
            "10",
        ],
    )
    assert result["mode"] == "open" and result["target_rate"] == 100
    assert result["requests"] == 100  # every scheduled request was sent
    assert result["errors"] == 0
    assert result["admitted"] == 10  # 5 per user, 2 users, nothing refills in 1s
    assert result["over_admitted"] is False
    check = result["limit_check_ms"]
    assert isinstance(check, dict) and 0 < check["p50"] < 50  # read from Server-Timing


def test_server_timing_parsing() -> None:
    from loadtest.loadtest import server_timing_ms

    assert server_timing_ms("ratelimit;dur=0.98") == 0.98
    assert server_timing_ms("db;dur=4, ratelimit;desc=x;dur=1.5") == 1.5
    assert server_timing_ms("db;dur=4") is None
    assert server_timing_ms("ratelimit;dur=fast") is None
    assert server_timing_ms(None) is None


def test_bounds_for_every_algorithm() -> None:
    spec = LimitSpec(10, 10)
    assert max_admissible(Algorithm.SLIDING_COUNTER, spec, 20) == 30  # per calendar window
    assert max_admissible(Algorithm.COUNTER, spec, 1000) == 10  # never resets
