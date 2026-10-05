"""FastAPI 入口：静态页面 + 筛查 / 见证 API。"""
from pathlib import Path

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .screening import GridInputError, compute_witness, run_screening, validate_grid

app = FastAPI(title="输电网单线退出风险筛查台", version="1.0.0")


@app.exception_handler(GridInputError)
async def grid_input_error_handler(_: Request, exc: GridInputError):
    return JSONResponse(status_code=400, content={"ok": False, "errors": exc.errors})


@app.post("/api/screen")
def api_screen(payload=Body(...)):
    """全量 N-1 筛查。请求体即电网 JSON（buses + lines）。"""
    grid = validate_grid(payload)
    return run_screening(grid)


@app.post("/api/witness")
def api_witness(payload=Body(...)):
    """按需提取指定 (退出线路, 在役线路) 的最坏注入见证并复核。

    请求体：{"grid": {...}, "outage": "L1", "line": "L2"}
    """
    if not isinstance(payload, dict) or "grid" not in payload:
        raise GridInputError(["请求体须包含 grid、outage、line 三个字段"])
    grid = validate_grid(payload["grid"])
    outage = payload.get("outage")
    line = payload.get("line")
    if not isinstance(outage, str) or not isinstance(line, str):
        raise GridInputError(["outage 与 line 必须是线路 id 字符串"])
    return compute_witness(grid, outage, line)


@app.get("/api/health")
def api_health():
    return {"ok": True}


# ---- 前端静态页面（挂载在最后，避免覆盖 API 路由）----
_HERE = Path(__file__).resolve()
for _cand in (_HERE.parents[2] / "frontend", _HERE.parents[1] / "frontend",
              Path.cwd() / "frontend"):
    if _cand.is_dir():
        app.mount("/", StaticFiles(directory=str(_cand), html=True), name="frontend")
        break
