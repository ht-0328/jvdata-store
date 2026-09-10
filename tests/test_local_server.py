"""操作パネルが、サーバーの状態を読み違えずに起動・停止できる。"""

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from jvstore.web import shutdown as shutdown_module
from jvstore.web.local_server import (
    LocalServer,
    ServerState,
    StartError,
    StopUnsupported,
)
from jvstore.web.server import Backend, _Server, make_handler

APP = "jvdata-store"


@pytest.fixture(autouse=True)
def quick_reservation(monkeypatch):
    monkeypatch.setattr(shutdown_module, "RESERVATION_POLL_SECONDS", 0.05)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def panel_for(port, tmp_path, command=None, app=APP):
    return LocalServer(app, port, command or [sys.executable, "-c", "pass"],
                       tmp_path, tmp_path / "serve.log")


def serve_in_thread(server):
    """本物の serve() と同じく、止まったら待ち受けのソケットも閉じる。"""
    def run():
        server.serve_forever()
        server.server_close()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def served(tmp_path):
    backend = Backend(tmp_path / "db.duckdb")
    server = _Server(("127.0.0.1", 0), make_handler(backend))
    thread = serve_in_thread(server)
    yield backend, thread, panel_for(server.server_address[1], tmp_path)
    server.shutdown()
    thread.join(timeout=5)


def test_nobody_listening_is_stopped(tmp_path):
    assert panel_for(free_port(), tmp_path).status().state is ServerState.STOPPED


def test_running_server_reports_its_database_and_task(served):
    backend, _, panel = served
    backend.task._begin("取得")
    status = panel.status()
    assert status.state is ServerState.RUNNING
    assert status.db == str(backend.db)
    assert status.task_running and status.task_label == "取得"
    assert not status.stop_reserved


def test_another_app_on_the_port_is_not_taken_for_ours(served, tmp_path):
    _, _, ours = served
    other = panel_for(ours.port, tmp_path, app="keiba-yosou")
    assert other.status().state is ServerState.OTHER_APP


def test_idle_server_stops(served):
    _, thread, panel = served
    assert panel.stop().stopped
    assert panel.status().state is ServerState.STOPPED
    assert not thread.is_alive()


def test_stop_is_refused_while_running_and_can_wait_for_the_task(served):
    backend, thread, panel = served
    backend.task._begin("取得")
    result = panel.stop()
    assert not result.stopped and result.running_label == "取得"
    panel.stop_after_task()
    assert panel.status().stop_reserved
    backend.task._finish(True)
    thread.join(timeout=5)
    assert panel.status().state is ServerState.STOPPED


def test_reservation_can_be_cancelled(served):
    backend, thread, panel = served
    backend.task._begin("取得")
    panel.stop_after_task()
    panel.cancel_stop()
    backend.task._finish(True)
    thread.join(timeout=0.5)
    assert panel.status().state is ServerState.RUNNING


class _OldServerHandler(BaseHTTPRequestHandler):
    """停止の窓口を持たない、以前の版のサーバー。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._reply(200, {"app": APP, "db": "old.duckdb"})

    def do_POST(self):
        self._reply(404, {"error": "not found"})

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_old_server_without_stop_is_explained(tmp_path):
    server = _Server(("127.0.0.1", 0), _OldServerHandler)
    thread = serve_in_thread(server)
    try:
        panel = panel_for(server.server_address[1], tmp_path)
        assert panel.status().state is ServerState.RUNNING
        with pytest.raises(StopUnsupported, match="Ctrl\\+C"):
            panel.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_start_runs_a_real_server_that_outlives_the_call(tmp_path):
    port = free_port()
    command = [sys.executable, "-m", "jvstore.cli", "serve",
               "--db", str(tmp_path / "db.duckdb"), "--port", str(port)]
    panel = panel_for(port, tmp_path, command)
    panel.start()
    try:
        assert panel.status().state is ServerState.RUNNING
    finally:
        assert panel.stop().stopped
    assert panel.status().state is ServerState.STOPPED


def test_start_failure_shows_the_reason_from_the_log(tmp_path):
    command = [sys.executable, "-c",
               "import sys; print('ポート 8766 は別の画面で使用中です。'); sys.exit(1)"]
    with pytest.raises(StartError, match="別の画面で使用中"):
        panel_for(free_port(), tmp_path, command).start()
