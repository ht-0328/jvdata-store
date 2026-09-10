"""サーバーの停止：処理の途中では止めず、予約すれば処理が終わってから止まる。"""

import json
import threading
from http.client import HTTPConnection

import pytest

from jvstore.web import shutdown as shutdown_module
from jvstore.web.server import Backend, _Server, make_handler

LABEL = "過去 1 年ぶんの取得"


@pytest.fixture(autouse=True)
def quick_reservation(monkeypatch):
    monkeypatch.setattr(shutdown_module, "RESERVATION_POLL_SECONDS", 0.05)


@pytest.fixture
def running(tmp_path):
    backend = Backend(tmp_path / "db.duckdb")
    server = _Server(("127.0.0.1", 0), make_handler(backend))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield backend, server, thread
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def request(server, method, path, headers=None):
    con = HTTPConnection(*server.server_address, timeout=5)
    try:
        con.request(method, path, headers=headers or {})
        response = con.getresponse()
        return response.status, json.loads(response.read())
    finally:
        con.close()


def has_stopped(thread, wait_seconds=5.0):
    thread.join(timeout=wait_seconds)
    return not thread.is_alive()


def test_idle_server_stops_and_starts_no_more_tasks(running):
    backend, server, thread = running
    assert request(server, "POST", "/api/shutdown") == (200, {"stopped": True})
    assert has_stopped(thread)
    assert not backend.task._begin("取得")


def test_running_task_is_never_cut_off(running):
    backend, server, thread = running
    backend.task._begin(LABEL)
    status, body = request(server, "POST", "/api/shutdown?when=now")
    assert status == 409
    assert body["running"] == LABEL
    assert LABEL in body["error"]
    assert not has_stopped(thread, 0.3)
    backend.task._finish(True)
    # 断られた停止は、次の処理を締め出さない。
    assert backend.task._begin("次の取得")


def test_reserved_stop_waits_for_the_task_and_then_stops(running):
    backend, server, thread = running
    backend.task._begin(LABEL)
    assert request(server, "POST", "/api/shutdown?when=after_task") == (
        200, {"stop_reserved": True})
    assert request(server, "GET", "/api/task")[1]["stop_reserved"] is True
    assert not has_stopped(thread, 0.3)
    backend.task._finish(True)
    assert has_stopped(thread)


def test_cancelled_reservation_keeps_the_server_running(running):
    backend, server, thread = running
    backend.task._begin(LABEL)
    request(server, "POST", "/api/shutdown?when=after_task")
    assert request(server, "POST", "/api/shutdown?when=cancel") == (
        200, {"stop_reserved": False})
    backend.task._finish(True)
    assert not has_stopped(thread, 0.5)
    assert request(server, "GET", "/api/task")[1]["stop_reserved"] is False


def test_reservation_can_be_made_again_after_cancel(running):
    backend, server, thread = running
    backend.task._begin(LABEL)
    for when in ("after_task", "cancel", "after_task"):
        request(server, "POST", f"/api/shutdown?when={when}")
    backend.task._finish(True)
    assert has_stopped(thread)


def test_unknown_mode_is_rejected(running):
    _, server, thread = running
    status, body = request(server, "POST", "/api/shutdown?when=later")
    assert status == 400 and body["error"]
    assert not has_stopped(thread, 0.3)


def test_the_screen_itself_can_stop_the_server(running):
    _, server, thread = running
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    assert request(server, "POST", "/api/shutdown", {"Origin": origin})[0] == 200
    assert has_stopped(thread)


@pytest.mark.parametrize("headers", [
    {"Origin": "https://example.com"},
    {"Origin": "null"},
    {"Host": "attacker.example"},
])
def test_other_sites_cannot_operate_the_server(running, headers):
    backend, server, thread = running
    status, _ = request(server, "POST", "/api/shutdown", headers)
    assert status == 403
    status, _ = request(server, "POST", "/api/history/fetch?years=1", headers)
    assert status == 403
    assert not has_stopped(thread, 0.3)
    assert backend.task.snapshot()["status"] == "idle"
