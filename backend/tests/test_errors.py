"""统一错误体契约（docs/01 §3.2）：所有非 2xx 响应同一外壳。M1（B6）起逐错误码补全。"""

from fastapi.testclient import TestClient

from bidqa.main import create_app


def test_not_found_uses_unified_body(settings_factory):
    client = TestClient(create_app(settings_factory()))
    r = client.get("/api/no-such-route")
    assert r.status_code == 404
    body = r.json()
    assert body["error_code"] == 4041
    assert body["message"]
    assert body["trace_id"]
    assert body["detail"] is None


def test_trace_id_unique_per_request(settings_factory):
    client = TestClient(create_app(settings_factory()))
    t1 = client.get("/api/no-such-route").json()["trace_id"]
    t2 = client.get("/api/no-such-route").json()["trace_id"]
    assert t1 != t2
