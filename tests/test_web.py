"""取得の入力、保存先、排他、失敗表示を実通信なしで検証する。"""

import json
import sys
import threading
from http.client import HTTPConnection

import pytest

from jvstore.web.server import Backend, _Server, make_handler
from jvstore.web.tasks import Task


def test_standalone_screen_serves_identity_and_html_without_a_database(tmp_path):
    backend = Backend(tmp_path / "not-created.duckdb")
    server = _Server(("127.0.0.1", 0), make_handler(backend))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    con = HTTPConnection(*server.server_address, timeout=5)
    try:
        con.request("GET", "/api/info")
        info = json.loads(con.getresponse().read())
        assert info == {"app": "jvdata-store", "db": str(backend.db)}
        con.request("GET", "/")
        response = con.getresponse()
        assert response.status == 200
        assert "JRA-VAN データ取得" in response.read().decode("utf-8")
        con.request("GET", "/api/history/tables")
        assert json.loads(con.getresponse().read())["ready"] is False
        with backend._lock:
            backend.task._begin("取得")
            con.request("GET", "/api/history/tables")
            response = con.getresponse()
            assert response.status == 409
            assert json.loads(response.read())["error"]
        backend.task._finish(True)
        assert not backend.db.exists()
    finally:
        con.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def api(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "history.duckdb")
    monkeypatch.setattr(backend, "dataspecs", lambda: [
        {"id": "RACE", "title": "レース"}, {"id": "DIFN", "title": "蓄積"},
    ])
    calls = []
    monkeypatch.setattr(backend.task, "start", lambda *a, **kw: calls.append((a, kw)) or True)
    server = _Server(("127.0.0.1", 0), make_handler(backend))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(query, path="/api/history/fetch"):
        con = HTTPConnection(*server.server_address, timeout=5)
        try:
            con.request("POST", path + "?" + query)
            response = con.getresponse()
            return response.status, json.loads(response.read())
        finally:
            con.close()

    yield backend, calls, post
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("force", [False, True])
def test_fetch_writes_to_browsed_database_and_forwards_mode(api, force):
    backend, calls, post = api
    status, result = post(f"years=10&dataspec=RACE,DIFN&force_setup={int(force)}")
    assert status == 200 and result == {"started": True}
    (label, steps, cwd), kwargs = calls[0]
    command = steps[0]
    assert command[command.index("--db") + 1] == str(backend.db.resolve())
    assert command[command.index("--dataspec") + 1] == "RACE,DIFN"
    assert ("--force-setup" in command) == force
    assert kwargs["db_lock"] is backend._lock
    assert command[:3] == [sys.executable, "-m", "jvstore.cli"]


@pytest.mark.parametrize("query", [
    "years=0", "years=41", "years=1.5", "years=abc",
    "dataspec=RACE,INVALID", "dataspec=INVALID", "force_setup=true",
])
def test_invalid_request_never_starts_acquisition(api, query):
    _, calls, post = api
    status, result = post(query)
    assert status == 400 and result["error"]
    assert calls == []


def test_realtime_fetch_runs_the_realtime_command_for_the_given_days(api):
    backend, calls, post = api
    status, result = post("date=2026-09-20&date=20260921", "/api/realtime/fetch")
    assert status == 200 and result == {"started": True}
    (label, steps, cwd), kwargs = calls[0]
    command = steps[0]
    assert command[:4] == [sys.executable, "-m", "jvstore.cli", "realtime"]
    assert command[command.index("--db") + 1] == str(backend.db.resolve())
    assert [command[i + 1] for i, word in enumerate(command) if word == "--date"] == ["20260920", "20260921"]
    assert "2026-09-20" in label and kwargs["db_lock"] is backend._lock


@pytest.mark.parametrize("query", ["", "date=2026-13-40", "date=abc", "&".join(f"date=2026-09-{d:02d}" for d in range(1, 9))])
def test_invalid_realtime_request_never_starts_acquisition(api, query):
    _, calls, post = api
    status, result = post(query, "/api/realtime/fetch")
    assert status == 400 and result["error"]
    assert calls == []


def test_process_failure_stops_later_steps_and_is_not_reported_as_success(tmp_path):
    task = Task()
    task._begin("取得")
    marker = tmp_path / "should-not-exist"
    task._run([
        [sys.executable, "-c", "import sys; print('fetch failed'); sys.exit(2)"],
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
    ], tmp_path, threading.Lock())
    result = task.snapshot()
    assert not marker.exists()
    assert result["status"] == "failed" and not result["running"]
    assert any("fetch failed" in line for line in result["log"])
    assert not any(line.startswith("完了") for line in result["log"])


def test_worker_exception_releases_database_lock(tmp_path, monkeypatch):
    task = Task()
    lock = threading.Lock()

    def fail(*args):
        raise OSError("uv unavailable")

    monkeypatch.setattr(task, "_stream", fail)
    task._begin("取得")
    task._run([["uv"]], tmp_path, lock)
    assert task.snapshot()["status"] == "failed"
    assert lock.acquire(blocking=False)
    lock.release()


def test_running_task_excludes_second_task_and_database_access(tmp_path, monkeypatch):
    task = Task()
    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()

    def stream(*args):
        entered.set()
        assert release.wait(5)
        return 0

    monkeypatch.setattr(task, "_stream", stream)
    assert task.start("取得", [["fake"]], tmp_path, lock)
    try:
        assert entered.wait(5)
        assert not task.start("重複取得", [["fake"]], tmp_path, lock)
        assert task.snapshot()["running"]
        assert not lock.acquire(blocking=False)
    finally:
        release.set()
    # DB lock is released before the final task snapshot is updated.
    assert lock.acquire(timeout=5)
    lock.release()


def test_successful_task_can_start_again(tmp_path):
    task = Task()
    for _ in range(2):
        assert task._begin("取得")
        task._run([[sys.executable, "-c", "print('done')"]], tmp_path, None)
        assert task.snapshot()["status"] == "succeeded"
        assert task.snapshot()["finished_at"]
