"""核心引擎正确性测试：手算案例 + 与 scipy LP 求解器交叉验证。"""
import numpy as np
import pytest
from scipy.optimize import linprog

from backend.app.screening import (
    GridInputError,
    _Base,
    _screen_one,
    compute_witness,
    run_screening,
    validate_grid,
)


def make_grid(buses, lines):
    return validate_grid({"buses": buses, "lines": lines})


def scenario(result, outage_id):
    return next(s for s in result["scenarios"] if s["outage"] == outage_id)


def line_entry(sc, line_id):
    i = sc["lines"]["id"].index(line_id)
    return {k: sc["lines"][k][i] for k in sc["lines"]}


# --------------------------------------------------------------------------- #
# 手算案例 1：三母线三角网，x=1、限额=10
#   B1 ∈ [-5, 15]，B2 ∈ [-10, 10]，B3 ∈ [-10, 10]
#   退出 L3(1-3) 后剩链 1-2-3：f_L1 = p1 → max|f| = 15（越限 1.5）；
#   f_L2 = -p3 → max|f| = 10（恰好达限，不算越限）
# --------------------------------------------------------------------------- #

TRI_BUSES = [
    {"id": "B1", "p_min": -5, "p_max": 15},
    {"id": "B2", "p_min": -10, "p_max": 10},
    {"id": "B3", "p_min": -10, "p_max": 10},
]
TRI_LINES = [
    {"id": "L1", "from": "B1", "to": "B2", "x": 1.0, "limit": 10.0},
    {"id": "L2", "from": "B2", "to": "B3", "x": 1.0, "limit": 10.0},
    {"id": "L3", "from": "B1", "to": "B3", "x": 1.0, "limit": 10.0},
]


def test_triangle_outage_L3():
    res = run_screening(make_grid(TRI_BUSES, TRI_LINES))
    sc = scenario(res, "L3")
    assert sc["status"] == "ok"
    e1 = line_entry(sc, "L1")
    assert e1["max_abs_flow"] == pytest.approx(15.0, abs=1e-9)
    assert e1["ratio"] == pytest.approx(1.5, abs=1e-9)
    assert e1["overload"] is True
    e2 = line_entry(sc, "L2")
    assert e2["max_abs_flow"] == pytest.approx(10.0, abs=1e-9)
    assert e2["ratio"] == pytest.approx(1.0, abs=1e-9)
    # 恰好达到限额不算越限
    assert e2["overload"] is False
    # 最危险线路为 L1，其见证使 f_L1 达到最坏值
    assert sc["worst"]["line"] == "L1"
    p = sc["worst"]["injections"]
    assert abs(sum(p.values())) < 1e-6                      # 零和
    for b in TRI_BUSES:                                     # 界内
        assert b["p_min"] - 1e-9 <= p[b["id"]] <= b["p_max"] + 1e-9
    assert abs(abs(sc["worst"]["flows"]["L1"]) - 15.0) < 1e-6


def test_triangle_outage_L1_symmetric():
    res = run_screening(make_grid(TRI_BUSES, TRI_LINES))
    sc = scenario(res, "L1")
    assert sc["status"] == "ok"
    # 剩链 2-1-3 与 1-3-2：L3 承载 p1 → max|f| = 15
    assert line_entry(sc, "L3")["max_abs_flow"] == pytest.approx(15.0, abs=1e-9)
    assert line_entry(sc, "L2")["max_abs_flow"] == pytest.approx(10.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# 手算案例 2：并行线路必须分别退出
# --------------------------------------------------------------------------- #

def test_parallel_lines_tripped_separately():
    g = make_grid(
        [
            {"id": "A", "p_min": -100, "p_max": 100},
            {"id": "B", "p_min": -100, "p_max": 100},
        ],
        [
            {"id": "P1", "from": "A", "to": "B", "x": 0.1, "limit": 50.0},
            {"id": "P2", "from": "A", "to": "B", "x": 0.1, "limit": 50.0},
        ],
    )
    res = run_screening(g)
    assert [s["outage"] for s in res["scenarios"]] == ["P1", "P2"]
    for out_id, inservice in (("P1", "P2"), ("P2", "P1")):
        sc = scenario(res, out_id)
        e = line_entry(sc, inservice)
        # 退出一条后，另一条承载全部 p_A：max|f| = 100 → 负载率 2.0
        assert e["max_abs_flow"] == pytest.approx(100.0, abs=1e-9)
        assert e["ratio"] == pytest.approx(2.0, abs=1e-9)
        assert e["overload"] is True


# --------------------------------------------------------------------------- #
# 手算案例 3：孤岛无法平衡 => 场景不可行；可平衡的多岛场景正常求解
#   链 1-2-3-4，B1 ∈ [1, 5]（必须发电），其余 ∈ [-3, 3]
# --------------------------------------------------------------------------- #

CHAIN_BUSES = [
    {"id": "B1", "p_min": 1, "p_max": 5},
    {"id": "B2", "p_min": -3, "p_max": 3},
    {"id": "B3", "p_min": -3, "p_max": 3},
    {"id": "B4", "p_min": -3, "p_max": 3},
]
CHAIN_LINES = [
    {"id": "L12", "from": "B1", "to": "B2", "x": 1.0, "limit": 10.0},
    {"id": "L23", "from": "B2", "to": "B3", "x": 1.0, "limit": 10.0},
    {"id": "L34", "from": "B3", "to": "B4", "x": 1.0, "limit": 10.0},
]


def test_islanding_infeasible_and_feasible():
    res = run_screening(make_grid(CHAIN_BUSES, CHAIN_LINES))
    # 退出 L12：孤岛 {B1} 注入区间 [1,5] 不含 0 → 不可行
    sc = scenario(res, "L12")
    assert sc["status"] == "infeasible"
    assert sc["lines"] is None and sc["worst"] is None
    bad = [i for i in sc["islands"] if not i["feasible"]]
    assert len(bad) == 1 and bad[0]["buses"] == ["B1"]
    # 退出 L23：岛 {B1,B2} 与 {B3,B4} 均可平衡
    sc = scenario(res, "L23")
    assert sc["status"] == "ok"
    assert len(sc["islands"]) == 2
    # 岛 {B1,B2}：f_L12 = p1，且 p1+p2=0、p2∈[-3,3] ⇒ p1∈[1,3] → max|f| = 3
    assert line_entry(sc, "L12")["max_abs_flow"] == pytest.approx(3.0, abs=1e-9)
    # 岛 {B3,B4}：f_L34 = -p4 ∈ [-3,3] → max|f| = 3
    assert line_entry(sc, "L34")["max_abs_flow"] == pytest.approx(3.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #

def _expect_error(data, needle):
    with pytest.raises(GridInputError) as ei:
        validate_grid(data)
    assert any(needle in e for e in ei.value.errors), ei.value.errors


def test_validation_duplicate_bus_id():
    _expect_error(
        {"buses": [{"id": "B1", "p_min": -1, "p_max": 1},
                    {"id": "B1", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "B1", "to": "B1", "x": 1, "limit": 1}]},
        "母线 ID 重复",
    )


def test_validation_duplicate_line_id():
    _expect_error(
        {"buses": [{"id": "A", "p_min": -1, "p_max": 1},
                    {"id": "B", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "B", "x": 1, "limit": 1},
                    {"id": "L1", "from": "A", "to": "B", "x": 2, "limit": 1}]},
        "线路 ID 重复",
    )


def test_validation_invalid_endpoint():
    _expect_error(
        {"buses": [{"id": "A", "p_min": -1, "p_max": 1},
                    {"id": "B", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "C", "x": 1, "limit": 1}]},
        "端点无效",
    )
    _expect_error(
        {"buses": [{"id": "A", "p_min": -1, "p_max": 1},
                    {"id": "B", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "A", "x": 1, "limit": 1}]},
        "起点与终点相同",
    )


def test_validation_inverted_bounds():
    _expect_error(
        {"buses": [{"id": "A", "p_min": 5, "p_max": -5},
                    {"id": "B", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "B", "x": 1, "limit": 1}]},
        "上下界倒置",
    )


def test_validation_nonpositive_reactance_and_limit():
    _expect_error(
        {"buses": [{"id": "A", "p_min": -1, "p_max": 1},
                    {"id": "B", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "B", "x": 0, "limit": 1}]},
        "电抗",
    )
    _expect_error(
        {"buses": [{"id": "A", "p_min": -1, "p_max": 1},
                    {"id": "B", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "B", "x": 1, "limit": -2}]},
        "热限额",
    )


def test_validation_base_disconnected():
    _expect_error(
        {"buses": [{"id": "A", "p_min": -1, "p_max": 1},
                    {"id": "B", "p_min": -1, "p_max": 1},
                    {"id": "C", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "B", "x": 1, "limit": 1}]},
        "不连通",
    )


def test_validation_base_unbalanced():
    _expect_error(
        {"buses": [{"id": "A", "p_min": 2, "p_max": 5},
                    {"id": "B", "p_min": -1, "p_max": 1}],
         "lines": [{"id": "L1", "from": "A", "to": "B", "x": 1, "limit": 10}]},
        "总注入为零",
    )


# --------------------------------------------------------------------------- #
# 与 scipy.optimize.linprog 交叉验证（随机小网，逐场景逐线路比对 max|f|）
# --------------------------------------------------------------------------- #

def _random_grid(rng, n=8, m=14):
    buses = []
    for i in range(n):
        lo = -rng.uniform(5, 40)
        hi = rng.uniform(5, 40)
        buses.append({"id": f"B{i}", "p_min": lo, "p_max": hi})
    edges = set()
    perm = list(range(n))
    rng.shuffle(perm)
    for i in range(1, n):
        a, b = perm[i], perm[rng.randrange(i)]
        edges.add((min(a, b), max(a, b)))
    while len(edges) < m:
        a, b = rng.randrange(n), rng.randrange(n)
        if a != b:
            edges.add((min(a, b), max(a, b)))
    lines = [
        {"id": f"L{k}", "from": f"B{a}", "to": f"B{b}",
         "x": rng.uniform(0.05, 0.5), "limit": rng.uniform(10, 60)}
        for k, (a, b) in enumerate(sorted(edges))
    ]
    return {"buses": buses, "lines": lines}


def _linprog_maxabs(g, k, j_line):
    """用通用 LP 求解器计算场景 k 下线路 j_line 的 max|f|（参照解）。"""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    act = [i for i in range(g.m) if i != k]
    fr, to = g.frm[act], g.to[act]
    adj = csr_matrix((np.ones(len(act)), (fr, to)), shape=(g.n, g.n))
    ncomp, labels = connected_components(adj, directed=False)
    sum_lo = np.zeros(ncomp)
    np.add.at(sum_lo, labels, g.p_min)
    sum_hi = np.zeros(ncomp)
    np.add.at(sum_hi, labels, g.p_max)
    if (sum_lo > 1e-9).any() or (sum_hi < -1e-9).any():
        return None  # 不可行
    w = 1.0 / g.x[act]
    B = np.zeros((g.n, g.n))
    np.add.at(B, (fr, fr), w)
    np.add.at(B, (to, to), w)
    np.add.at(B, (fr, to), -w)
    np.add.at(B, (to, fr), -w)
    Bp = np.linalg.pinv(B, hermitian=True)
    H = w[:, None] * (Bp[fr] - Bp[to])
    j = act.index(j_line)
    c = H[j]
    # 逐岛等式约束
    A_eq = np.zeros((ncomp, g.n))
    for cc in range(ncomp):
        A_eq[cc, labels == cc] = 1.0
    bounds = list(zip(g.p_min, g.p_max))
    r1 = linprog(-c, A_eq=A_eq, b_eq=np.zeros(ncomp), bounds=bounds,
                 method="highs")
    r2 = linprog(c, A_eq=A_eq, b_eq=np.zeros(ncomp), bounds=bounds,
                 method="highs")
    assert r1.success and r2.success
    return max(-r1.fun, -r2.fun)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_crosscheck_with_linprog(seed):
    import random
    data = _random_grid(random.Random(seed))
    g = make_grid(data["buses"], data["lines"])
    base = _Base(g)
    for k in range(g.m):
        res = _screen_one(base, k)
        if res.status != "ok":
            continue
        for t, j_line in enumerate(res.act):
            ref = _linprog_maxabs(g, k, int(j_line))
            assert res.max_abs[t] == pytest.approx(ref, abs=1e-6), (
                f"seed={seed} outage={k} line={j_line}: "
                f"ours={res.max_abs[t]} linprog={ref}"
            )


@pytest.mark.parametrize("seed", [3, 4])
def test_worst_witnesses_independently_verified(seed):
    """逐场景复核最坏见证：界内、逐岛零和、由见证独立重算的 |f| 与报告值一致。"""
    import random
    data = _random_grid(random.Random(seed))
    g = make_grid(data["buses"], data["lines"])
    out = run_screening(g)
    for sc in out["scenarios"]:
        if sc["status"] != "ok" or sc["worst"] is None:
            continue
        w = sc["worst"]
        p = np.array([w["injections"][b] for b in g.bus_ids])
        assert (p >= g.p_min - 1e-6).all() and (p <= g.p_max + 1e-6).all()
        # 独立重算：退出线路后的 B，按岛 pinv 求 θ，再求全部线路潮流
        k = g.line_ids.index(sc["outage"])
        act = [i for i in range(g.m) if i != k]
        fr, to = g.frm[act], g.to[act]
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import connected_components
        adj = csr_matrix((np.ones(len(act)), (fr, to)), shape=(g.n, g.n))
        ncomp, labels = connected_components(adj, directed=False)
        for c in range(ncomp):
            assert abs(p[labels == c].sum()) < 1e-5
        wv = 1.0 / g.x[act]
        B = np.zeros((g.n, g.n))
        np.add.at(B, (fr, fr), wv)
        np.add.at(B, (to, to), wv)
        np.add.at(B, (fr, to), -wv)
        np.add.at(B, (to, fr), -wv)
        theta = np.linalg.pinv(B, hermitian=True) @ p
        flows = wv * (theta[fr] - theta[to])
        j = [g.line_ids[i] for i in act].index(w["line"])
        assert abs(abs(flows[j]) - w["max_abs_flow"]) < 1e-5
        # 见证潮流与响应中附带的 flows 一致
        for i, lid in enumerate([g.line_ids[t] for t in act]):
            assert abs(flows[i] - w["flows"][lid]) < 1e-4


# --------------------------------------------------------------------------- #
# 见证端点复核
# --------------------------------------------------------------------------- #

def test_witness_endpoint_checks():
    g = make_grid(TRI_BUSES, TRI_LINES)
    w = compute_witness(g, "L3", "L1")
    assert w["status"] == "ok"
    assert w["checks"]["bounds_ok"] is True
    assert all(b["ok"] for b in w["checks"]["island_balance"])
    assert w["checks"]["flow_consistency_max_err"] < 1e-9
    assert w["witness"]["ratio"] == pytest.approx(1.5, abs=1e-9)
    # 不可行场景的见证请求
    g2 = make_grid(CHAIN_BUSES, CHAIN_LINES)
    w3 = compute_witness(g2, "L12", "L23")
    assert w3["status"] == "infeasible"


def test_witnesses_may_differ_per_line():
    """各线路的最坏见证可以不同：退出 L12 后，岛 {B2,B3,B4} 中
    L23 的最坏见证为 p=[-8,2,6]，L34 的最坏见证为 p=[1,5,-6]。"""
    g = make_grid(
        [
            {"id": "B1", "p_min": -1, "p_max": 1},
            {"id": "B2", "p_min": -8, "p_max": 2},
            {"id": "B3", "p_min": -5, "p_max": 5},
            {"id": "B4", "p_min": -6, "p_max": 6},
        ],
        [
            {"id": "L12", "from": "B1", "to": "B2", "x": 1.0, "limit": 100.0},
            {"id": "L23", "from": "B2", "to": "B3", "x": 1.0, "limit": 100.0},
            {"id": "L34", "from": "B3", "to": "B4", "x": 1.0, "limit": 100.0},
        ],
    )
    w23 = compute_witness(g, "L12", "L23")
    w34 = compute_witness(g, "L12", "L34")
    assert w23["status"] == "ok" and w34["status"] == "ok"
    # f_L23 = p2 → max|f| = 8；f_L34 = -p4 → max|f| = 6
    assert w23["witness"]["max_abs_flow"] == pytest.approx(8.0, abs=1e-9)
    assert w34["witness"]["max_abs_flow"] == pytest.approx(6.0, abs=1e-9)
    p23 = w23["witness"]["injections"]
    p34 = w34["witness"]["injections"]
    # 两个不同的最优顶点：L23 最坏见证 p=[-8,2,6]，L34 最坏见证 p=[1,5,-6]
    assert [p23["B2"], p23["B3"], p23["B4"]] == pytest.approx([-8, 2, 6], abs=1e-6)
    assert [p34["B2"], p34["B3"], p34["B4"]] == pytest.approx([1, 5, -6], abs=1e-6)
    assert any(abs(p23[b] - p34[b]) > 1e-6 for b in p23)
