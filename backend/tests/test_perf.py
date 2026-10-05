"""性能测试：100 母线 / 200 线路全量 N-1 筛查须在 5 秒内完成。"""
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.screening import run_screening, validate_grid
from examples.gen_case import gen_case


@pytest.fixture(scope="module")
def big_result():
    data = gen_case(100, 200, seed=1)
    g = validate_grid(data)
    t0 = time.perf_counter()
    res = run_screening(g)
    elapsed = time.perf_counter() - t0
    return res, elapsed


def test_full_screening_within_5s(big_result):
    res, elapsed = big_result
    assert res["meta"]["scenarios"] == 200
    assert res["meta"]["buses"] == 100
    assert elapsed < 5.0, f"全量筛查耗时 {elapsed:.2f}s，超过 5s 限制"
    print(f"\n100 母线 / 200 线路全量筛查耗时 {elapsed:.2f}s")


def test_results_complete_and_consistent(big_result):
    res, _ = big_result
    assert len(res["scenarios"]) == 200
    for sc in res["scenarios"]:
        if sc["status"] != "ok":
            continue
        lines = sc["lines"]
        assert len(lines["id"]) == 199            # 每条在役线路都有最坏负载率
        w = sc["worst"]
        assert w is not None
        i = lines["id"].index(w["line"])
        assert lines["ratio"][i] == w["ratio"]    # 最危险线路即负载率最高者
        assert w["ratio"] == max(lines["ratio"])
        # 见证零和
        assert abs(sum(w["injections"].values())) < 1e-4
