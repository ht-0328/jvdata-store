"""操作パネルから、画面のサーバーを起動・停止・開き直す。

サーバーとは HTTP（`/api/info`・`/api/task`・`/api/shutdown`）だけでやり取りする。
パネルが自分で起動したサーバーでなくても、同じポートで動いていれば操作できる。
ウィンドウ（tkinter）はここに持ち込まない。見た目と切り離して試せるようにするため。

keiba-yosou にも同じ実装を置き、互いの Python パッケージには依存しない。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

#: 起動を待つ上限（秒）。
START_TIMEOUT_SECONDS = 30
#: 停止してポートが空くのを待つ上限（秒）。
STOP_TIMEOUT_SECONDS = 10
#: 起動・停止を待つあいだに状態を確かめる間隔（秒）。
_WAIT_POLL_SECONDS = 0.3
#: 1回の問い合わせを待つ上限（秒）。
_REQUEST_TIMEOUT_SECONDS = 3
#: 起動に失敗したとき、ログの末尾をこの行数だけ見せる。
_LOG_TAIL_LINES = 15

#: パネルを閉じてもサーバーが止まらないよう、別のプロセスグループで動かす。
#: 取得の子プロセスが黒い窓を出さないよう、見えないコンソールを持たせる。
_DETACHED_FLAGS = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


class ServerState(StrEnum):
    STOPPED = "stopped"          # ポートで誰も待ち受けていない
    RUNNING = "running"          # このアプリのサーバーが動いている
    OTHER_APP = "other_app"      # 別のアプリがポートを使っている
    NOT_RESPONDING = "not_responding"  # 待ち受けているが、時間内に応えない


@dataclass(frozen=True)
class ServerStatus:
    state: ServerState
    db: str = ""
    task_label: str = ""
    task_running: bool = False
    stop_reserved: bool = False


@dataclass(frozen=True)
class StopResult:
    stopped: bool
    #: 断られたとき、実行中だった処理の名前。
    running_label: str = ""


def _port_is_free(port: int) -> bool:
    """そのポートで誰も待ち受けていなければ True。

    Windows では、閉じたポートへの接続が拒否されるまで約2秒かかる。
    先に自分で bind できるかを試せば、止まっていることが一瞬で分かる。
    bind できなかったときは HTTP で確かめる（こちらが正、これは近道）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def console_python() -> str:
    """サーバーを動かす Python。

    パネルは黒い窓を出さない `pythonw.exe` で動くが、サーバーまでそれで動かすと、
    取得の子プロセスの出力を受け取る経路が不安定になる。隣の `python.exe` を使い、
    窓は `CREATE_NO_WINDOW` で隠す。
    """
    executable = Path(sys.executable)
    console = executable.with_name("python.exe")
    if executable.name.lower() == "pythonw.exe" and console.exists():
        return str(console)
    return str(executable)


class StartError(RuntimeError):
    """サーバーが起動しなかった。メッセージにログの末尾を含める。"""


class StopUnsupported(RuntimeError):
    """停止の窓口を持たない、古い版のサーバーが動いている。"""


class LocalServer:
    """1つのポートで動く、画面のサーバー。"""

    def __init__(self, app: str, port: int, command: list[str], cwd: Path,
                 log_path: Path, page_query: str = "") -> None:
        self.app = app
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self._command = command
        self._cwd = cwd
        self._log_path = log_path
        self._page_query = page_query

    def status(self) -> ServerStatus:
        if _port_is_free(self.port):
            return ServerStatus(ServerState.STOPPED)
        try:
            code, info = self._request("GET", "api/info")
        except ConnectionRefusedError:
            return ServerStatus(ServerState.STOPPED)
        except (http.client.HTTPException, ValueError):
            return ServerStatus(ServerState.OTHER_APP)
        except OSError:
            return ServerStatus(ServerState.NOT_RESPONDING)
        if code != 200 or info.get("app") != self.app:
            return ServerStatus(ServerState.OTHER_APP)
        try:
            _, task = self._request("GET", "api/task")
        except (OSError, http.client.HTTPException, ValueError):
            task = {}
        return ServerStatus(
            ServerState.RUNNING,
            db=str(info.get("db", "")),
            task_label=str(task.get("label", "")),
            task_running=bool(task.get("running")),
            stop_reserved=bool(task.get("stop_reserved")),
        )

    def start(self) -> None:
        """サーバーを別プロセスで起動し、応答するまで待つ。"""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("wb") as log:
            process = subprocess.Popen(
                self._command, cwd=self._cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, creationflags=_DETACHED_FLAGS,
            )
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.status().state is ServerState.RUNNING:
                return
            if process.poll() is not None:
                raise StartError(f"起動できませんでした。\n\n{self._log_tail()}")
            time.sleep(_WAIT_POLL_SECONDS)
        raise StartError("起動に時間がかかっています。少し待ってから状態を確かめてください。"
                         f"\n\n{self._log_tail()}")

    def open_in_browser(self) -> None:
        webbrowser.open(self.url + self._page_query)

    def stop(self) -> StopResult:
        """処理が動いていなければ止める。動いていれば止めずに、その処理の名前を返す。"""
        code, payload = self._shutdown("now")
        if code == 409:
            return StopResult(stopped=False, running_label=str(payload.get("running", "")))
        self._wait_until_stopped()
        return StopResult(stopped=True)

    def stop_after_task(self) -> None:
        self._shutdown("after_task")

    def cancel_stop(self) -> None:
        self._shutdown("cancel")

    def _shutdown(self, when: str) -> tuple[int, dict[str, Any]]:
        code, payload = self._request("POST", f"api/shutdown?when={when}")
        if code == 404:
            raise StopUnsupported(
                "このサーバーは停止の操作に対応していない版です。"
                "起動したコンソールで Ctrl+C を押して止めてください。")
        if code not in (200, 409):
            raise RuntimeError(str(payload.get("error") or f"HTTP {code}"))
        return code, payload

    def _wait_until_stopped(self) -> None:
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.status().state is ServerState.STOPPED:
                return
            time.sleep(_WAIT_POLL_SECONDS)

    def _request(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
        """応答コードと JSON を返す。つながらないときは OSError を投げる。

        4xx・5xx も応答として返す。停止を断られた（409）ことは例外ではなく結果だから。
        """
        request = Request(self.url + path, method=method)
        try:
            with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                body = error.read()
            try:
                return error.code, json.loads(body)
            except ValueError:
                return error.code, {}
        except URLError as error:
            # urlopen は接続の失敗を URLError で包む。中身の OSError を出して、
            # 呼ぶ側が「誰もいない（拒否）」と「応答が無い」を見分けられるようにする。
            if isinstance(error.reason, OSError):
                raise error.reason from error
            raise

    def _log_tail(self) -> str:
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "（ログを読めませんでした）"
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-_LOG_TAIL_LINES:]) or "（ログは空です）"
