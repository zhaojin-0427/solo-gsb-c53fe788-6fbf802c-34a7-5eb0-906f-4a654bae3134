# 输电网单线退出（N-1）风险筛查台

面向值班员的交互式 N-1 安全筛查 Web 工具：上传电网数据（母线净注入上下界 +
线路端点/正电抗/热限额），系统在**全部可行注入方案**下逐条退出每条线路，
求每条在役线路的**最大绝对潮流（最坏负载率）**，并给出**可复核的注入见证**。

- 后端：Python / FastAPI / NumPy / SciPy
- 前端：原生 JavaScript（无框架、无构建步骤）
- 部署：Docker Compose 一键启动

---

## 1. 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动后访问 **http://localhost:8000** 即进入筛查台页面：

1. 点击 **加载示例**（31 母线 / 48 线路，含并行线路、径向负荷孤岛）；
2. 点击 **开始筛查**；
3. 在左侧**退出场景表**中点选任意线路退出场景，右侧拓扑图按各线路最坏
   负载率着色（红色 = 越限，虚线 = 退出线路，粗线 = 最危险线路）；
4. 在**场景详情**中点选任意在役线路，下方展示该线路的**最坏注入见证**
   （各母线注入、相角 θ、逐线潮流）及**独立复核结果**。

> 资源限制：compose 已配置 `cpus: 2.0 / memory: 1G`，与交付指标一致。

### 本地开发启动（不用 Docker）

```bash
pip install -r backend/requirements-dev.txt
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
# 打开 http://localhost:8000
```

运行测试：

```bash
python -m pytest backend/tests/ -q
```

---

## 2. 输入数据格式

请求体为一个 JSON 对象（页面文本框粘贴、文件上传，或直接调 API）：

```json
{
  "buses": [
    {"id": "B1", "p_min": -100.0, "p_max": 150.0},
    {"id": "B2", "p_min": -80.0,  "p_max": 0.0}
  ],
  "lines": [
    {"id": "L1", "from": "B1", "to": "B2", "x": 0.1, "limit": 50.0}
  ]
}
```

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `buses[].id` | 母线 ID（字符串/数字） | 全网唯一，非空 |
| `buses[].p_min` / `p_max` | 母线净注入下/上界（MW，注入为正） | 有限数值，`p_min ≤ p_max` |
| `lines[].id` | 线路 ID | 全网唯一，非空 |
| `lines[].from` / `to` | 线路两端母线 ID | 必须是已声明母线，且起止不同 |
| `lines[].x` | 线路电抗（标幺/欧姆，与潮流单位自洽即可） | **正**有限数 |
| `lines[].limit` | 线路热限额（MW） | **正**有限数 |
| `buses[].x` / `buses[].y` | （可选）母线绘图坐标 | 提供则拓扑图直接使用 |

**结构性强校验（不满足即判输入错误，HTTP 400）：**

- 母线/线路 **ID 重复**；
- 线路**端点无效**（引用不存在的母线、起止相同）；
- 注入**上下界倒置**（`p_min > p_max`）；
- **电抗或热限额非正**（含缺失、非有限数）；
- **基态网络不连通**（存在多个电气孤岛）；
- **基态不存在总注入为零的可行方案**（`Σp_min > 0` 或 `Σp_max < 0`）。

所有错误一次性以中文逐条报出。

---

## 3. 数学模型与结果口径

**DC 潮流模型。** 对每个退出场景（退出线路 k）：

```
B·θ = p                            节点平衡（每个电气孤岛内 Σp = 0）
f_l = (θ_from(l) − θ_to(l)) / x_l  线路潮流
```

**最坏潮流。** 在所有满足 `p_min ≤ p ≤ p_max` 且逐岛零和的注入方案 p 中，
对每条在役线路 l 分别求 `max |f_l|`（正、反两个方向各自求解取大者）。
负载率 = `max|f_l| / limit_l`。

**结果口径（与需求逐条对应）：**

- **逐线最坏负载率**：每个场景给出全部在役线路的最坏负载率；
- **最危险线路**：每个场景负载率最高的线路，附达到该值的注入见证；
- **各线路的最坏见证可以不同**：每条线路的最坏值独立优化、各有见证，
  页面点选任意线路即可调取其专属见证；
- **不可行场景**：任一孤岛不存在零和平衡注入（`Σp_min > 0` 或
  `Σp_max < 0`）时，该退出场景整体标记为**不可行**，不给出线路结果；
- **并行线路分别退出**：并行线路是不同的线路实体，各自独立成场景，
  退出其中一条时其余并行线路仍在役；
- **恰好达到限额不算越限**：仅当 `max|f| > limit × (1 + 1e-9)` 才判越限，
  等于限额判为"达限"（不越限）。

**注入见证的可复核性。** 见证接口返回：各母线注入 p、各母线相角 θ、
各线路潮流 f，以及后端独立复核结果——注入界内性（最大越界量）、
逐岛注入和、f 与 `(θ起−θ终)/x` 的最大偏差。值班员可用 θ 手工验算
任意线路潮流。

---

## 4. 算法（为什么能在 5 秒内算完）

直接对 `(场景 × 线路 × 2 方向) ≈ 8 万个 LP` 调通用求解器不可行。本工具
利用两个结构性质精确求解，无需 LP 求解器、无需逐场景矩阵分解：

1. **盒式界 + 逐岛单等式的 LP 有阈值结构解。**
   `max c·p, s.t. Σp=0, L≤p≤U` 的最优解：按 c 降序，高者取上界、低者
   取下界、至多一个变量取残差使总和为零。排序后扫描前缀和即得精确解
   （`O(n log n)`），且约束按岛解耦、逐岛独立求解。见证即该最优顶点。

2. **退出线路的 PTDF 只做秩-1 修正（Woodbury / LODF）。**
   退出线路 k 后 `B_k = B − w_k·u·uᵀ`（u = e_a − e_b）：
   - 非桥线路：`B_k⁺ = B⁺ + β·z·zᵀ`，`β = w_k/(1 − w_k·uᵀB⁺u)`，
     `z = B⁺u`——即线路开断分布因子（LODF）关系，每场景 `O(m·n)`；
   - 桥线路（`1 − w_k·R_eff ≈ 0`，网络解列为两岛）：按岛构造
     `B_k⁺ = (B_k + P)⁻¹ − P`（P 为各岛 `1/n_s` 全一块），严格处理孤岛。

   全量计算只做**一次**基态伪逆。

**复杂度**：`O(m·n·log n + m²·n)` ≈ 200²·100 量级，实测（2 核 / 1GiB
容器规格，单线程 BLAS）：

| 算例 | 规模 | 后端全量用时 |
| --- | --- | --- |
| 示例算例 | 31 母线 / 48 线路 | ≈ 0.05 s |
| 交付指标算例 | **100 母线 / 200 线路** | **≈ 0.3–0.5 s（< 5 s 指标）** |
| 极端：全桥树状网 | 100 母线 / 99 线路（99 次解列） | ≈ 0.2 s |

> 注：应用默认以单线程 BLAS 运行（大量小矩阵运算下多线程反而引入
> 调度抖动），Docker 镜像与应用包初始化均已固化该设置。

---

## 5. API

### `POST /api/screen`

请求体：电网 JSON（见 §2）。响应：

```json
{
  "ok": true,
  "meta": {"buses": 31, "lines": 48, "scenarios": 48,
           "infeasible_scenarios": 1, "overload_scenarios": 17,
           "max_ratio": 6.0596, "elapsed_ms": 50.2},
  "scenarios": [
    {"outage": "L1", "status": "ok",
     "islands": null,
     "lines": {"id": ["L2", ...], "max_abs_flow": [...], "limit": [...],
               "ratio": [...], "overload": [...], "direction": [...]},
     "worst": {"line": "L9", "direction": 1, "flow": 121.3,
               "max_abs_flow": 121.3, "limit": 20.0, "ratio": 6.06,
               "overload": true,
               "injections": {"B1": ...}, "flows": {"L2": ...}}}
  ]
}
```

- `status`: `ok` / `infeasible`（不可行时 `lines`、`worst` 为 null，
  `islands` 给出各岛 `Σp_min/Σp_max` 及可行性）；
- `islands`: `null` 表示全网单岛；否则为逐岛信息列表；
- `direction`: +1 最坏值来自正向潮流，−1 来自反向；
- `worst.injections` / `worst.flows`：最危险线路的注入见证及其潮流分布。

### `POST /api/witness`

请求体：`{"grid": {...}, "outage": "L1", "line": "L2"}`。响应在
`witness` 中额外给出 `angles`（各母线相角 θ），并在 `checks` 中给出
独立复核结果：

```json
{"ok": true, "status": "ok", "witness": {...},
 "checks": {"bounds_ok": true, "bounds_max_violation": 0.0,
            "island_balance": [{"buses": [...], "sum_injection": 0.0, "ok": true}],
            "flow_consistency_max_err": 0.0,
            "ratio_recomputed": 1.5,
            "at_limit_not_overload": false}}
```

### 错误响应

输入校验失败返回 **HTTP 400**：

```json
{"ok": false, "errors": ["母线 ID 重复：'B1'", "线路 'L3' 电抗 x 必须为正有限数"]}
```

---

## 6. 算例与生成器

- `frontend/sample_grid.json`：页面"加载示例"使用的 31 母线 / 48 线路
  算例（含 2 条并行线路、1 条径向负荷线路——其退出场景不可行）；
- `examples/case_100_200.json`：100 母线 / 200 线路性能算例；
- `examples/gen_case.py`：算例生成器。

```bash
# 生成自定义规模算例（--margin 按基态最坏潮流的倍数设定限额）
python examples/gen_case.py --buses 100 --lines 200 --seed 1 \
    --margin 1.3 2.2 --parallel 2 --radial-load --out my_case.json
```

---

## 7. 项目结构

```
├── docker-compose.yml      # 一键启动（含 2 核 / 1GiB 资源限制）
├── Dockerfile
├── backend/
│   ├── app/
│   │   ├── main.py         # FastAPI 路由与静态页面托管
│   │   └── screening.py    # 筛查引擎（校验 / DC 潮流 / 最坏潮流 / 见证）
│   ├── tests/              # 手算案例、linprog 交叉验证、见证复核、性能
│   └── requirements.txt
├── frontend/
│   ├── index.html          # 单页应用（输入 / 场景表 / 拓扑图 / 见证复核）
│   ├── app.js
│   ├── styles.css
│   └── sample_grid.json
└── examples/               # 算例生成器与性能算例
```

## 8. 测试

```bash
python -m pytest backend/tests/ -q
```

- `test_screening.py`：手算精确案例（三角网、并行线路、解列不可行、
  恰好达限不判越限、各线路见证不同）、随机小网与
  `scipy.optimize.linprog` 的逐场景逐线路交叉验证、最坏见证独立复核；
- `test_api.py`：API 正常/错误路径；
- `test_perf.py`：**100 母线 / 200 线路全量筛查 < 5 s** 的交付指标回归。
