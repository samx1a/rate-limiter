import json

import pytest

from bench import bench


def test_memory_benchmark_runs(capsys: pytest.CaptureFixture[str]) -> None:
    bench.main(["--calls", "200", "--repeats", "1"])
    report = json.loads(capsys.readouterr().out)
    assert set(report["memory"]) == {
        "counter",
        "fixed_window",
        "sliding_window",
        "sliding_counter",
        "token_bucket",
    }
    assert all(v > 0 for row in report["memory"].values() for v in row.values())


@pytest.mark.redis
def test_redis_benchmark_runs(redis_client: object, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.conftest import REDIS_URL

    bench.main(["--calls", "10", "--redis-calls", "10", "--repeats", "1", "--redis", REDIS_URL])
    report = json.loads(capsys.readouterr().out)
    assert set(report["redis"]) == {
        "ping_us",
        "one_key_us",
        "two_keys_one_by_one_us",
        "two_keys_batched_us",
    }
