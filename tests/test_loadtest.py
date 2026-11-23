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
