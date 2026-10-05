"""生成测试用电网算例（JSON）。

用法：
    python examples/gen_case.py --buses 100 --lines 200 --seed 1 --out case.json
    python examples/gen_case.py --buses 30 --lines 45 --seed 7 \
        --radial-load --parallel 2 --out sample.json

选项：
    --radial-load   追加一条径向“负荷孤岛”线路，用于演示退出后孤岛无法平衡
                    （该退出场景会被标记为不可行）
    --parallel N    随机复制 N 条并行线路（同端点、不同 id），
                    用于演示并行线路须分别退出
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _limits_from_base_worst(case: dict, margin_lo: float, margin_hi: float,
                            seed: int) -> None:
    """按基态（无退出）各线路最坏 |潮流| 的 [margin_lo, margin_hi] 倍设置限额，
    使 N-1 场景下出现有意义的越限分布。"""
    from backend.app.screening import _Base, _maxmin_bounded_balance, validate_grid

    g = validate_grid(case)
    base = _Base(g)
    op, _, on, _, _ = _maxmin_bounded_balance(base.H, g.p_min, g.p_max)
    base_worst = [max(a, b) for a, b in zip(op, on)]
    rng = random.Random(seed)
    for j, ln in enumerate(case["lines"]):
        ln["limit"] = round(max(base_worst[j], 1e-6) * rng.uniform(margin_lo, margin_hi), 2)


def gen_case(n_buses: int, n_lines: int, seed: int,
             radial_load: bool = False, n_parallel: int = 0,
             margin: tuple[float, float] | None = None) -> dict:
    rng = random.Random(seed)

    buses = []
    for i in range(n_buses):
        lo = -round(rng.uniform(30.0, 120.0), 2)   # 最大受电
        hi = round(rng.uniform(20.0, 150.0), 2)    # 最大发电
        buses.append({"id": f"B{i + 1}", "p_min": lo, "p_max": hi})

    # 随机生成树保证连通，再补足线路数
    perm = list(range(n_buses))
    rng.shuffle(perm)
    edges: set[tuple[int, int]] = set()
    for i in range(1, n_buses):
        a, b = perm[i], perm[rng.randrange(i)]
        edges.add((min(a, b), max(a, b)))
    while len(edges) < n_lines:
        a, b = rng.randrange(n_buses), rng.randrange(n_buses)
        if a != b:
            edges.add((min(a, b), max(a, b)))

    lines = []
    for i, (a, b) in enumerate(sorted(edges)):
        lines.append({
            "id": f"L{i + 1}",
            "from": f"B{a + 1}",
            "to": f"B{b + 1}",
            "x": round(rng.uniform(0.02, 0.30), 4),
            "limit": round(rng.uniform(45.0, 110.0), 2),
        })

    # 并行线路：复制若干已有线路的端点（不同 id、不同参数）
    for j in range(n_parallel):
        src = lines[rng.randrange(len(lines))]
        lines.append({
            "id": f"L{len(lines) + 1}",
            "from": src["from"],
            "to": src["to"],
            "x": round(rng.uniform(0.02, 0.30), 4),
            "limit": round(rng.uniform(45.0, 110.0), 2),
        })

    # 径向负荷孤岛：新增母线只能受电，靠唯一线路相连
    if radial_load:
        host = rng.randrange(n_buses)
        nid = n_buses + 1
        buses.append({"id": f"B{nid}", "p_min": -40.0, "p_max": -10.0})
        lines.append({
            "id": f"L{len(lines) + 1}",
            "from": f"B{host + 1}",
            "to": f"B{nid}",
            "x": 0.05,
            "limit": 80.0,
        })

    if margin is not None:
        _limits_from_base_worst({"buses": buses, "lines": lines},
                                margin[0], margin[1], seed + 1)

    return {"buses": buses, "lines": lines}


def main():
    ap = argparse.ArgumentParser(description="生成输电网 N-1 筛查测试算例")
    ap.add_argument("--buses", type=int, default=100)
    ap.add_argument("--lines", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--radial-load", action="store_true")
    ap.add_argument("--parallel", type=int, default=0)
    ap.add_argument("--margin", type=float, nargs=2, default=None,
                    metavar=("LO", "HI"),
                    help="按基态最坏潮流的 LO~HI 倍设置限额（推荐 1.3 2.2）")
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    case = gen_case(args.buses, args.lines, args.seed,
                    radial_load=args.radial_load, n_parallel=args.parallel,
                    margin=tuple(args.margin) if args.margin else None)
    text = json.dumps(case, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"已写出 {args.out}：{len(case['buses'])} 母线 / {len(case['lines'])} 线路")
    else:
        print(text)


if __name__ == "__main__":
    main()
