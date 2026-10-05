"""API 层测试：输入错误 400、筛查 200、见证端点。"""
from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)

GRID = {
    "buses": [
        {"id": "B1", "p_min": -5, "p_max": 15},
        {"id": "B2", "p_min": -10, "p_max": 10},
        {"id": "B3", "p_min": -10, "p_max": 10},
    ],
    "lines": [
        {"id": "L1", "from": "B1", "to": "B2", "x": 1.0, "limit": 10.0},
        {"id": "L2", "from": "B2", "to": "B3", "x": 1.0, "limit": 10.0},
        {"id": "L3", "from": "B1", "to": "B3", "x": 1.0, "limit": 10.0},
    ],
}


def test_screen_ok():
    r = client.post("/api/screen", json=GRID)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["meta"]["scenarios"] == 3
    sc = next(s for s in body["scenarios"] if s["outage"] == "L3")
    assert sc["worst"]["line"] == "L1"
    assert sc["worst"]["ratio"] == 1.5


def test_screen_input_error_400():
    bad = {
        "buses": [{"id": "B1", "p_min": 5, "p_max": -5}],
        "lines": [{"id": "L1", "from": "B1", "to": "B9", "x": -1, "limit": 0}],
    }
    r = client.post("/api/screen", json=bad)
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    text = "；".join(body["errors"]) if "；" in str(body) else " ".join(body["errors"])
    assert "上下界倒置" in text
    assert "端点无效" in text
    assert "电抗" in text
    assert "热限额" in text


def test_witness_ok():
    r = client.post("/api/witness", json={"grid": GRID, "outage": "L3", "line": "L1"})
    assert r.status_code == 200
    body = r.json()
    assert body["witness"]["ratio"] == 1.5
    assert body["checks"]["bounds_ok"] is True
    assert abs(sum(body["witness"]["injections"].values())) < 1e-6


def test_witness_bad_line():
    r = client.post("/api/witness", json={"grid": GRID, "outage": "L3", "line": "L9"})
    assert r.status_code == 400


def test_frontend_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
