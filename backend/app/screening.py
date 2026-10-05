"""输电网单线退出（N-1）风险筛查核心引擎。

模型（DC 潮流）：
    B θ = p                              节点平衡（每个电气孤岛内注入之和为零）
    f_l = (θ_from(l) − θ_to(l)) / x_l    线路潮流

在全部可行注入方案（p_min ≤ p ≤ p_max，逐岛零和）下，对每条在役线路
分别求 |f_l| 的最大值（最坏潮流）及达到该值的注入见证。各线路的最坏
见证互不相同、各自独立求解。

两个关键结构使得全量筛查无需通用 LP 求解器、无需逐场景矩阵分解：

1. 目标 max c·p 的约束（逐岛零和 + 盒式界）按岛解耦；每个岛是
   “单等式 + 盒式界”的 LP，最优解具有阈值结构（c_i 大于阈值取上界、
   小于阈值取下界、至多一个变量取中间值），按 c 排序扫描前缀和即可
   精确求解，复杂度 O(s·log s)。

2. 退出线路 k 后节点导纳阵 B_k = B − w_k·u·uᵀ（u = e_a − e_b）：
   - 非桥线路：由伪逆的 Woodbury 恒等式（即 LODF 关系）
     B_k⁺ = B⁺ + β·z·zᵀ，β = w_k/(1 − w_k·uᵀB⁺u)，z = B⁺u，
     PTDF 矩阵只做一次秩-1 修正，O(m·n)；
   - 桥线路（1 − w_k·uᵀB⁺u ≈ 0，网络解列为两岛）：按岛构造
     B_k⁺ = (B_k + P)⁻¹ − P（P 为各岛 1/n_s 全一块），每岛一次求逆。
"""
from __future__ import annotations

import time

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

# 恰好达到限额不算越限：仅当 max|f| > limit * (1 + OVERLOAD_RTOL) 才判越限
OVERLOAD_RTOL = 1e-9
# 零和平衡可行性判定的相对容差
BALANCE_RTOL = 1e-9
# 桥线路判定：1 − w_k·R_eff ≤ BRIDGE_RTOL 视为解列（走分块求逆路径，对任意
# 连通性均严格正确，阈值只影响性能不影响正确性）
BRIDGE_RTOL = 1e-9


class GridInputError(Exception):
    """输入数据错误（重复 ID、无效端点、上下界倒置、非正电抗/限额、
    基态不连通或不存在零和注入方案等）。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class Grid:
    """校验后的电网数据（全部转为 numpy 数组）。"""

    def __init__(self, bus_ids, p_min, p_max, line_ids, frm, to, x, limit):
        self.bus_ids = bus_ids
        self.p_min = np.asarray(p_min, dtype=float)
        self.p_max = np.asarray(p_max, dtype=float)
        self.line_ids = line_ids
        self.frm = np.asarray(frm, dtype=int)
        self.to = np.asarray(to, dtype=int)
        self.x = np.asarray(x, dtype=float)
        self.limit = np.asarray(limit, dtype=float)

    @property
    def n(self) -> int:
        return len(self.bus_ids)

    @property
    def m(self) -> int:
        return len(self.line_ids)


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #

def _is_finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v)


def validate_grid(data) -> Grid:
    """校验输入 JSON，全部错误一次性以中文报出；通过则返回 Grid。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise GridInputError(["请求体必须是 JSON 对象，且包含 buses 与 lines 两个数组"])
    buses = data.get("buses")
    lines = data.get("lines")
    if not isinstance(buses, list) or len(buses) == 0:
        errors.append("缺少母线列表：buses 必须是非空数组")
        buses = []
    if not isinstance(lines, list) or len(lines) == 0:
        errors.append("缺少线路列表：lines 必须是非空数组")
        lines = []

    # ---- 母线 ----
    bus_ids: list[str] = []
    seen_buses: set[str] = set()
    for i, b in enumerate(buses):
        if not isinstance(b, dict):
            errors.append(f"buses[{i}] 必须是对象（含 id、p_min、p_max）")
            continue
        bid = b.get("id")
        if not isinstance(bid, (str, int)) or isinstance(bid, bool) or str(bid).strip() == "":
            errors.append(f"buses[{i}] 缺少有效的母线 id")
            continue
        bid = str(bid)
        if bid in seen_buses:
            errors.append(f"母线 ID 重复：'{bid}'")
        else:
            seen_buses.add(bid)
        lo, hi = b.get("p_min"), b.get("p_max")
        if not _is_finite_number(lo):
            errors.append(f"母线 '{bid}' 的 p_min 缺失或不是有限数值")
        if not _is_finite_number(hi):
            errors.append(f"母线 '{bid}' 的 p_max 缺失或不是有限数值")
        if _is_finite_number(lo) and _is_finite_number(hi) and lo > hi:
            errors.append(f"母线 '{bid}' 注入上下界倒置：p_min({lo}) > p_max({hi})")
        bus_ids.append(bid)

    # ---- 线路 ----
    line_ids: list[str] = []
    seen_lines: set[str] = set()
    bus_set = set(bus_ids)
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            errors.append(f"lines[{i}] 必须是对象（含 id、from、to、x、limit）")
            continue
        lid = ln.get("id")
        if not isinstance(lid, (str, int)) or isinstance(lid, bool) or str(lid).strip() == "":
            errors.append(f"lines[{i}] 缺少有效的线路 id")
            continue
        lid = str(lid)
        if lid in seen_lines:
            errors.append(f"线路 ID 重复：'{lid}'")
        else:
            seen_lines.add(lid)
        f, t = ln.get("from"), ln.get("to")
        f = str(f) if isinstance(f, (str, int)) and not isinstance(f, bool) else None
        t = str(t) if isinstance(t, (str, int)) and not isinstance(t, bool) else None
        if f is None or f not in bus_set:
            errors.append(f"线路 '{lid}' 起点端点无效：'{ln.get('from')}' 不是已声明的母线")
        if t is None or t not in bus_set:
            errors.append(f"线路 '{lid}' 终点端点无效：'{ln.get('to')}' 不是已声明的母线")
        if f is not None and t is not None and f == t:
            errors.append(f"线路 '{lid}' 起点与终点相同（无效端点）")
        x, lim = ln.get("x"), ln.get("limit")
        if not _is_finite_number(x) or x <= 0:
            errors.append(f"线路 '{lid}' 电抗 x 必须为正有限数")
        if not _is_finite_number(lim) or lim <= 0:
            errors.append(f"线路 '{lid}' 热限额 limit 必须为正有限数")
        line_ids.append(lid)

    if errors:
        raise GridInputError(errors)

    # ---- 组装数组 ----
    bus_index = {bid: i for i, bid in enumerate(bus_ids)}
    p_min = [float(b["p_min"]) for b in buses]
    p_max = [float(b["p_max"]) for b in buses]
    frm = [bus_index[str(ln["from"])] for ln in lines]
    to = [bus_index[str(ln["to"])] for ln in lines]
    x = [float(ln["x"]) for ln in lines]
    limit = [float(ln["limit"]) for ln in lines]
    g = Grid(bus_ids, p_min, p_max, line_ids, frm, to, x, limit)

    # ---- 基态结构要求：连通 ----
    adj = csr_matrix(
        (np.ones(g.m, dtype=np.int8), (g.frm, g.to)), shape=(g.n, g.n)
    )
    ncomp, _ = connected_components(adj, directed=False)
    if ncomp != 1:
        errors.append(f"基态网络不连通：存在 {ncomp} 个电气孤岛，要求全网联通")

    # ---- 基态结构要求：存在总注入为零的可行方案 ⟺ Σp_min ≤ 0 ≤ Σp_max ----
    sum_lo, sum_hi = float(g.p_min.sum()), float(g.p_max.sum())
    tol = BALANCE_RTOL * max(1.0, abs(sum_lo), abs(sum_hi))
    if sum_lo > tol:
        errors.append(f"基态不存在总注入为零的可行注入方案：Σp_min = {sum_lo:g} > 0")
    if sum_hi < -tol:
        errors.append(f"基态不存在总注入为零的可行注入方案：Σp_max = {sum_hi:g} < 0")

    if errors:
        raise GridInputError(errors)
    return g


# --------------------------------------------------------------------------- #
# 盒式界 + 单等式约束 LP 的精确贪心求解
# --------------------------------------------------------------------------- #

def _greedy_sorted(Cs: np.ndarray, Us: np.ndarray, Ls: np.ndarray):
    """对已在系数降序域中的 (Cs, Us, Ls) 求解 max Cs·p s.t. Ls≤p≤Us, Σp=0。

    返回 (obj (r,), Ps (r, s))；Ps 为排序域中的最优注入（见证）。
    调用前须保证可行（ΣLs ≤ 0 ≤ ΣUs）。
    """
    r, s = Cs.shape
    cumU = np.cumsum(Us, axis=1)
    cumL = np.cumsum(Ls, axis=1)
    sumL = cumL[:, -1]
    # T[k] = 前 k+1 个取上界、其余取下界的总和；关于 k 单调不降
    T = cumU + (sumL[:, None] - cumL)
    scale = np.maximum(1.0, np.maximum(np.abs(sumL), np.abs(cumU[:, -1])))
    k = (T >= -BALANCE_RTOL * scale[:, None]).argmax(axis=1)   # 首个 ≥0 位置
    rows = np.arange(r)
    prev_cumU = np.where(k > 0, cumU[rows, np.clip(k - 1, 0, None)], 0.0)
    p_k = -(prev_cumU + sumL - cumL[rows, k])           # 第 k 个取残差使 Σp = 0
    idx = np.arange(s)[None, :]
    Ps = np.where(idx < k[:, None], Us, Ls)
    Ps[rows, k] = p_k
    obj = np.einsum("ij,ij->i", Cs, Ps)
    return obj, Ps


def _maxmin_bounded_balance(C: np.ndarray, lo: np.ndarray, hi: np.ndarray):
    """一次排序同时求解 max c·p 与 max(−c·p)（等价 −min c·p）。

    返回 (obj_pos, Ps_pos, obj_neg, Ps_neg, order)；Ps_* 的第 j 行与
    order 的第 j 行对齐（排序域），需要时按 order 还原到母线顺序。
    """
    r, s = C.shape
    order = np.argsort(-C, axis=1, kind="stable")       # c 降序
    Cs = np.take_along_axis(C, order, axis=1)
    Us = hi[order]
    Ls = lo[order]
    obj_pos, Ps_pos = _greedy_sorted(Cs, Us, Ls)
    # −c 的降序排列 = c 降序排列的反转；解向量反转回 order 域对齐
    obj_neg, Ps_neg = _greedy_sorted(-Cs[:, ::-1], Us[:, ::-1], Ls[:, ::-1])
    return obj_pos, Ps_pos, obj_neg, Ps_neg[:, ::-1], order


# --------------------------------------------------------------------------- #
# 基态预计算
# --------------------------------------------------------------------------- #

class _Base:
    """基态预计算：节点导纳阵 B、伪逆 B⁺、PTDF 矩阵 H、邻接表。"""

    def __init__(self, g: Grid):
        self.g = g
        w = 1.0 / g.x
        B = np.zeros((g.n, g.n))
        np.add.at(B, (g.frm, g.frm), w)
        np.add.at(B, (g.to, g.to), w)
        np.add.at(B, (g.frm, g.to), -w)
        np.add.at(B, (g.to, g.frm), -w)
        self.w = w
        self.B = B
        self.Bp = np.linalg.pinv(B, hermitian=True)
        # H[j] = (e_fr − e_to)ᵀ B⁺ / x_j，f = H·p
        self.H = w[:, None] * (self.Bp[g.frm] - self.Bp[g.to])
        # 邻接表（桥判定后的 BFS 用）
        self.adj = [[] for _ in range(g.n)]
        for j in range(g.m):
            self.adj[g.frm[j]].append((g.to[j], j))
            self.adj[g.to[j]].append((g.frm[j], j))


# --------------------------------------------------------------------------- #
# 单场景（退出第 k 条线路）计算
# --------------------------------------------------------------------------- #

class _Scenario:
    """单场景内部计算结果（含见证提取所需的全部上下文）。"""

    __slots__ = (
        "outage", "status", "islands", "act", "labels",
        "H", "obj_pos", "obj_neg", "max_abs", "ratio", "overload", "direction",
        "island_sols", "theta_ctx",
    )

    def __init__(self, outage, status, islands):
        self.outage = outage
        self.status = status            # "ok" | "infeasible"
        self.islands = islands          # None 表示全网单岛；否则逐岛信息列表


def _bfs_component(base: _Base, start: int, skip_line: int) -> np.ndarray:
    """在去掉线路 skip_line 的图中从 start 做 BFS，返回可达节点集合。"""
    seen = np.zeros(base.g.n, dtype=bool)
    stack = [start]
    seen[start] = True
    while stack:
        v = stack.pop()
        for nb, j in base.adj[v]:
            if j != skip_line and not seen[nb]:
                seen[nb] = True
                stack.append(nb)
    return seen


def _island_info(g: Grid, labels: np.ndarray, ncomp: int):
    """逐岛可行性：Σp_min ≤ 0 ≤ Σp_max。任一岛不满足则场景不可行。"""
    sum_lo = np.zeros(ncomp)
    np.add.at(sum_lo, labels, g.p_min)
    sum_hi = np.zeros(ncomp)
    np.add.at(sum_hi, labels, g.p_max)
    tol = BALANCE_RTOL * np.maximum(1.0, np.maximum(np.abs(sum_lo), np.abs(sum_hi)))
    islands = []
    all_ok = True
    for c in range(ncomp):
        idx = np.nonzero(labels == c)[0]
        ok = bool(sum_lo[c] <= tol[c] and sum_hi[c] >= -tol[c])
        all_ok &= ok
        islands.append({
            "buses": [g.bus_ids[i] for i in sorted(idx.tolist())],
            "sum_min": round(float(sum_lo[c]), 6),
            "sum_max": round(float(sum_hi[c]), 6),
            "feasible": ok,
        })
    return islands, all_ok


def _solve_islands(g: Grid, H: np.ndarray, labels: np.ndarray, ncomp: int):
    """逐岛调用贪心 LP，汇总每条在役线路正/反向最坏值及排序域见证。"""
    r = H.shape[0]
    obj_pos = np.zeros(r)
    obj_neg = np.zeros(r)
    island_sols = []
    for c in range(ncomp):
        idx = np.nonzero(labels == c)[0]
        o1, p1, o2, p2, order = _maxmin_bounded_balance(
            H[:, idx], g.p_min[idx], g.p_max[idx]
        )
        obj_pos += o1
        obj_neg += o2
        island_sols.append((idx, order, p1, p2))
    return obj_pos, obj_neg, island_sols


def _screen_one(base: _Base, k: int) -> _Scenario:
    """退出第 k 条线路后的全线路最坏潮流计算（并行线路各自独立退出）。"""
    g = base.g
    n, m = g.n, g.m
    act = np.nonzero(np.arange(m) != k)[0]
    fr, to = g.frm[act], g.to[act]
    a, b = g.frm[k], g.to[k]
    wk = base.w[k]

    # ---- 桥判定：denom = 1 − w_k·R_eff(a,b) ----
    z = base.Bp[:, a] - base.Bp[:, b]
    denom = 1.0 - wk * (z[a] - z[b])

    if denom > BRIDGE_RTOL:
        # 非桥：全网仍单岛（基态可行性已保证），PTDF 秩-1 修正
        beta = wk / denom
        dz = base.w * (z[g.frm] - z[g.to])
        H = base.H[act] + beta * np.outer(dz[act], z)
        labels = np.zeros(n, dtype=int)
        ncomp = 1
        islands = None
        theta_ctx = ("rank1", z, beta)
    else:
        # 桥：解列为两岛，逐岛检验零和可行性
        comp_a = _bfs_component(base, a, k)
        labels = np.where(comp_a, 0, 1)
        ncomp = 2
        islands, all_ok = _island_info(g, labels, ncomp)
        if not all_ok:
            res = _Scenario(k, "infeasible", islands)
            return res
        # B_k⁺ = (B_k + P)⁻¹ − P，P 为各岛 1/n_s 全一块
        u = np.zeros(n)
        u[a], u[b] = 1.0, -1.0
        Bk = base.B - wk * np.outer(u, u)
        P = np.zeros((n, n))
        for c in range(ncomp):
            idx = np.nonzero(labels == c)[0]
            P[np.ix_(idx, idx)] = 1.0 / len(idx)
        Bp_k = np.linalg.inv(Bk + P) - P
        H = base.w[act][:, None] * (Bp_k[fr] - Bp_k[to])
        theta_ctx = ("matrix", Bp_k)

    res = _Scenario(k, "ok", islands)
    res.act = act
    res.labels = labels
    res.H = H
    res.theta_ctx = theta_ctx

    r = len(act)
    if r == 0:
        res.obj_pos = res.obj_neg = np.zeros(0)
        res.max_abs = np.zeros(0)
        res.ratio = np.zeros(0)
        res.overload = np.zeros(0, dtype=bool)
        res.direction = np.zeros(0, dtype=int)
        res.island_sols = []
        return res

    obj_pos, obj_neg, island_sols = _solve_islands(g, H, labels, ncomp)
    res.obj_pos, res.obj_neg = obj_pos, obj_neg
    res.island_sols = island_sols
    res.max_abs = np.maximum(obj_pos, obj_neg)
    res.direction = np.where(obj_pos >= obj_neg, 1, -1)  # 最坏值来自正向还是反向
    res.ratio = res.max_abs / g.limit[act]
    res.overload = res.max_abs > g.limit[act] * (1.0 + OVERLOAD_RTOL)  # 恰好达限不算越限
    return res


def _reconstruct_p(res: _Scenario, j: int, direction: int, n: int) -> np.ndarray:
    """把排序域见证还原为母线顺序的注入向量（仅对需要的线路调用）。"""
    p = np.zeros(n)
    for idx, order, p_pos, p_neg in res.island_sols:
        Ps = p_pos if direction == 1 else p_neg
        p[idx[order[j]]] = Ps[j]
    return p


def _theta(res: _Scenario, p: np.ndarray, base: _Base) -> np.ndarray:
    """由注入求相角（用于见证复核：f = (θ起 − θ终)/x）。"""
    kind = res.theta_ctx[0]
    if kind == "rank1":
        _, z, beta = res.theta_ctx
        return base.Bp @ p + beta * z * (z @ p)
    _, Bp_k = res.theta_ctx
    return Bp_k @ p


# --------------------------------------------------------------------------- #
# 结果格式化
# --------------------------------------------------------------------------- #

def _worst_index(res: _Scenario):
    """最坏（负载率最高）在役线路在 act 中的位置；无在役线路返回 None。"""
    if res.status != "ok" or len(res.act) == 0:
        return None
    return int(np.argmax(res.ratio))


def _witness_dict(g: Grid, base: _Base, res: _Scenario, j: int,
                  with_angles=False) -> dict:
    """提取 act 中第 j 条线路的最坏注入见证（达到 max|f| 的可行注入方案）。"""
    d = int(res.direction[j])
    p = _reconstruct_p(res, j, d, g.n)
    flows = res.H @ p
    act_ids = [g.line_ids[i] for i in res.act]
    out = {
        "line": g.line_ids[res.act[j]],
        "direction": d,                              # +1 正向最坏 / −1 反向最坏
        "flow": round(float(res.obj_pos[j] if d == 1 else -res.obj_neg[j]), 6),
        "max_abs_flow": round(float(res.max_abs[j]), 6),
        "limit": round(float(g.limit[res.act[j]]), 6),
        "ratio": round(float(res.ratio[j]), 6),
        "overload": bool(res.overload[j]),
        "injections": dict(zip(g.bus_ids, np.round(p, 9).tolist())),
        "flows": dict(zip(act_ids, np.round(flows, 6).tolist())),
    }
    if with_angles:
        theta = _theta(res, p, base)
        out["angles"] = dict(zip(g.bus_ids, np.round(theta, 9).tolist()))
    return out


def _format_scenario(g: Grid, base: _Base, res: _Scenario) -> dict:
    out = {
        "outage": g.line_ids[res.outage],
        "status": res.status,
        "islands": res.islands,          # None 表示全网单岛
    }
    if res.status != "ok":
        out["lines"] = None
        out["worst"] = None
        return out
    act = res.act
    # 列式存储，压缩响应体积
    out["lines"] = {
        "id": [g.line_ids[i] for i in act],
        "max_abs_flow": np.round(res.max_abs, 6).tolist(),
        "limit": np.round(g.limit[act], 6).tolist(),
        "ratio": np.round(res.ratio, 6).tolist(),
        "overload": res.overload.tolist(),
        "direction": res.direction.astype(int).tolist(),
    }
    j = _worst_index(res)
    out["worst"] = _witness_dict(g, base, res, j) if j is not None else None
    return out


def run_screening(g: Grid) -> dict:
    """全量 N-1 筛查：逐条退出每条线路（并行线路各自独立退出）。"""
    t0 = time.perf_counter()
    base = _Base(g)
    scenarios = [_format_scenario(g, base, _screen_one(base, k)) for k in range(g.m)]
    elapsed = time.perf_counter() - t0
    n_infeasible = sum(1 for s in scenarios if s["status"] != "ok")
    n_overload = sum(
        1 for s in scenarios
        if s["lines"] is not None and any(s["lines"]["overload"])
    )
    ratios = [s["worst"]["ratio"] for s in scenarios if s["worst"] is not None]
    return {
        "ok": True,
        "meta": {
            "buses": g.n,
            "lines": g.m,
            "scenarios": len(scenarios),
            "infeasible_scenarios": n_infeasible,
            "overload_scenarios": n_overload,
            "max_ratio": round(max(ratios), 6) if ratios else 0.0,
            "elapsed_ms": round(elapsed * 1000.0, 1),
        },
        "scenarios": scenarios,
    }


def compute_witness(g: Grid, outage_id: str, line_id: str) -> dict:
    """按需重算某场景下指定线路的最坏注入见证，并给出独立复核结果。"""
    if outage_id not in g.line_ids:
        raise GridInputError([f"退出线路 '{outage_id}' 不存在"])
    k = g.line_ids.index(outage_id)
    base = _Base(g)
    res = _screen_one(base, k)
    if res.status != "ok":
        return {
            "ok": True, "outage": outage_id, "line": line_id,
            "status": "infeasible", "islands": res.islands,
            "witness": None, "checks": None,
        }
    act_ids = [g.line_ids[i] for i in res.act]
    if line_id not in act_ids:
        raise GridInputError([f"线路 '{line_id}' 在该场景下不在役"])
    j = act_ids.index(line_id)
    w = _witness_dict(g, base, res, j, with_angles=True)

    # ---- 复核：界内性、逐岛零和、潮流与 f=(θ起−θ终)/x 的一致性 ----
    d = w["direction"]
    p = _reconstruct_p(res, j, d, g.n)
    theta = _theta(res, p, base)
    bounds_viol = float(
        max(0.0, float(np.max(g.p_min - p)), float(np.max(p - g.p_max)))
    )
    ncomp = int(res.labels.max()) + 1
    island_balance = []
    for c in range(ncomp):
        idx = np.nonzero(res.labels == c)[0]
        s = float(p[idx].sum())
        island_balance.append({
            "buses": [g.bus_ids[i] for i in sorted(idx.tolist())],
            "sum_injection": round(s, 9),
            "ok": bool(abs(s) <= 1e-6 * max(1.0, float(np.abs(p[idx]).sum()))),
        })
    flows = res.H @ p
    fr, to = g.frm[res.act], g.to[res.act]
    flows_from_theta = (theta[fr] - theta[to]) / g.x[res.act]
    flow_err = float(np.max(np.abs(flows - flows_from_theta))) if len(res.act) else 0.0
    lim = float(g.limit[res.act[j]])
    checks = {
        "bounds_ok": bounds_viol <= 1e-6,
        "bounds_max_violation": round(bounds_viol, 9),
        "island_balance": island_balance,
        "flow_consistency_max_err": round(flow_err, 9),
        "ratio_recomputed": round(float(res.max_abs[j]) / lim, 6),
        "at_limit_not_overload": bool(
            abs(float(res.max_abs[j]) - lim) <= lim * OVERLOAD_RTOL
            and not res.overload[j]
        ),
    }
    return {
        "ok": True,
        "outage": outage_id,
        "line": line_id,
        "status": "ok",
        "islands": res.islands,
        "witness": w,
        "checks": checks,
    }
