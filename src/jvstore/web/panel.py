"""操作パネル：画面のサーバーを起動・停止・開き直す小さなウィンドウ。

ブラウザは、止まっているサーバーを起動できない。タブを閉じたあとに開き直す
ボタンも置けない。そこで起動・開き直し・停止は、このウィンドウで行う。

「処理の途中では止めない」という規則はサーバーが持つ（`shutdown.py`）。
パネルは断られた結果を伝え、利用者に「終わったら止める」かどうかを尋ねるだけ。

keiba-yosou にも同じ実装を置き、互いの Python パッケージには依存しない。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any

from .local_server import LocalServer, ServerState, ServerStatus, StopResult

#: 状態を確かめ直す間隔（ミリ秒）。
_REFRESH_MS = 1000
#: 裏で動かした操作の結果を拾う間隔（ミリ秒）。
_DRAIN_MS = 100

_FOOTNOTE_LONGEST_LINE = "操作の説明は、ブラウザの画面右上の「使い方」にあります。"
_FOOTNOTE = "このウィンドウを閉じても、サーバーは止まりません。\n" + _FOOTNOTE_LONGEST_LINE

_STATE_TEXT = {
    ServerState.STOPPED: ("停止中", "#8b8177"),
    ServerState.RUNNING: ("起動中", "#2e8b57"),
    ServerState.OTHER_APP: ("別のアプリが使用中", "#c0392b"),
    ServerState.NOT_RESPONDING: ("応答なし", "#c0392b"),
}


class ControlPanel:
    """1つのサーバーを操作するウィンドウ。"""

    def __init__(self, root: tk.Tk, server: LocalServer, app_title: str) -> None:
        self._root = root
        self._server = server
        self._results: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._busy = False
        self._checking = False
        self._status = ServerStatus(ServerState.STOPPED)

        root.title(f"{app_title} 操作パネル")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=16)
        frame.grid()
        # 折り返し幅は、いちばん長い固定の文言に合わせる。画素で決め打ちすると、
        # 画面の拡大率によって「開きます／。」のような半端な位置で折れる。
        wrap = tkfont.nametofont("TkDefaultFont").measure(_FOOTNOTE_LONGEST_LINE)

        self._state_label = ttk.Label(frame, font=("", 12, "bold"))
        self._state_label.grid(row=0, sticky="w")
        ttk.Label(frame, text=server.url).grid(row=1, sticky="w")
        self._detail = ttk.Label(frame, wraplength=wrap, justify="left")
        self._detail.grid(row=2, sticky="w", pady=(6, 0))

        self._reservation = ttk.Frame(frame)
        self._reservation.grid(row=3, sticky="w", pady=(8, 0))
        self._reservation_text = ttk.Label(self._reservation, foreground="#b7791f",
                                           wraplength=wrap, justify="left")
        self._reservation_text.grid(row=0, sticky="w")
        ttk.Button(self._reservation, text="予約を取り消す",
                   command=self._cancel_stop).grid(row=1, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, sticky="w", pady=(12, 8))
        self._start_button = ttk.Button(buttons, text="起動", command=self._start)
        self._open_button = ttk.Button(buttons, text="画面を開く", command=self._open)
        self._stop_button = ttk.Button(buttons, text="停止", command=self._stop)
        for button in (self._start_button, self._open_button, self._stop_button):
            button.pack(side="left", padx=(0, 8))

        ttk.Label(frame, foreground="#6b645c", justify="left",
                  text=_FOOTNOTE).grid(row=5, sticky="w")

        self._render()
        root.after(_DRAIN_MS, self._drain)

    # -- 起動したときの動き ------------------------------------------------

    def start_if_stopped(self) -> None:
        """止まっていれば起動して画面を開く。run.bat のダブルクリック1回で使えるように。"""
        def check_then_start() -> ServerStatus:
            status = self._server.status()
            if status.state is ServerState.STOPPED:
                self._server.start()
                self._server.open_in_browser()
                return self._server.status()
            return status
        self._run(check_then_start, self._on_start_done, busy_text="確認しています…")
        self._root.after(_REFRESH_MS, self._refresh)

    # -- ボタン ------------------------------------------------------------

    def _start(self) -> None:
        def start_and_open() -> ServerStatus:
            self._server.start()
            self._server.open_in_browser()
            return self._server.status()
        self._run(start_and_open, self._on_start_done, busy_text="起動しています…")

    def _open(self) -> None:
        self._server.open_in_browser()

    def _stop(self) -> None:
        self._run(self._server.stop, self._on_stop_done, busy_text="停止しています…")

    def _cancel_stop(self) -> None:
        self._run(self._server.cancel_stop, self._on_request_done)

    # -- 結果の受け取り ----------------------------------------------------

    def _on_request_done(self, result: Any) -> None:
        if isinstance(result, Exception):
            messagebox.showerror("操作できませんでした", str(result), parent=self._root)
        self._refresh_now()

    def _on_start_done(self, result: ServerStatus | Exception) -> None:
        if isinstance(result, Exception):
            messagebox.showerror("起動できませんでした", str(result), parent=self._root)
            return
        self._status = result
        self._render()

    def _on_stop_done(self, result: StopResult | Exception) -> None:
        if isinstance(result, Exception):
            messagebox.showerror("停止できませんでした", str(result), parent=self._root)
            return
        if result.stopped:
            self._refresh_now()
            return
        wait = messagebox.askyesno(
            "処理の途中です",
            f"「{result.running_label}」を実行中のため、いまは停止できません。\n\n"
            "終わってから停止しますか？\n\n"
            "はい：終わったら自動で停止します。\n"
            "いいえ：停止しません。",
            parent=self._root,
        )
        if wait:
            self._run(self._server.stop_after_task, self._on_request_done)

    # -- 状態の表示 --------------------------------------------------------

    def _refresh(self) -> None:
        self._refresh_now()
        self._root.after(_REFRESH_MS, self._refresh)

    def _refresh_now(self) -> None:
        if self._checking or self._busy:
            return
        self._checking = True

        def done(result: ServerStatus | Exception) -> None:
            self._checking = False
            if isinstance(result, ServerStatus):
                self._status = result
                self._render()

        self._in_background(self._server.status, done)

    def _render(self, busy_text: str = "") -> None:
        status = self._status
        text, color = _STATE_TEXT[status.state]
        self._state_label.configure(text=f"● {busy_text or text}", foreground=color)
        self._detail.configure(text=self._detail_text(status))

        if status.stop_reserved:
            self._reservation_text.configure(
                text=f"「{status.task_label}」が終わったら停止します。")
            self._reservation.grid()
        else:
            self._reservation.grid_remove()

        running = status.state is ServerState.RUNNING
        responding = running or status.state is ServerState.NOT_RESPONDING
        self._enable(self._start_button,
                     not self._busy and status.state is ServerState.STOPPED)
        self._enable(self._open_button, not self._busy and responding)
        self._enable(self._stop_button,
                     not self._busy and running and not status.stop_reserved)

    def _detail_text(self, status: ServerStatus) -> str:
        port = self._server.port
        if status.state is ServerState.STOPPED:
            return "「起動」を押すと、サーバーを立ち上げて画面を開きます。"
        if status.state is ServerState.OTHER_APP:
            return (f"ポート {port} は別のアプリが使っています。"
                    f"run.bat --port {port + 100} のように、別のポートを指定してください。")
        if status.state is ServerState.NOT_RESPONDING:
            return f"ポート {port} は使われていますが、応答がありません。しばらく待ってください。"
        lines = [f"保存先：{status.db}"]
        if status.task_running:
            lines.append(f"処理：{status.task_label}（実行中）")
        return "\n".join(lines)

    @staticmethod
    def _enable(button: ttk.Button, enabled: bool) -> None:
        button.state(["!disabled"] if enabled else ["disabled"])

    # -- 裏での実行 --------------------------------------------------------

    def _run(self, action: Callable[[], Any], on_done: Callable[[Any], None],
             busy_text: str = "") -> None:
        """操作を裏で動かし、終わるまでボタンを止める。起動は数秒かかるため。"""
        self._busy = True
        self._render(busy_text)

        def finish(result: Any) -> None:
            self._busy = False
            self._render()
            on_done(result)

        self._in_background(action, finish)

    def _in_background(self, action: Callable[[], Any],
                       on_done: Callable[[Any], None]) -> None:
        def work() -> None:
            try:
                result: Any = action()
            except Exception as error:  # noqa: BLE001  取りこぼすとボタンが止まったままになる
                result = error
            self._results.put((on_done, result))
        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        # tkinter は作ったスレッドからしか触れない。結果はキューで受け取る。
        while not self._results.empty():
            on_done, result = self._results.get_nowait()
            on_done(result)
        self._root.after(_DRAIN_MS, self._drain)


def run_panel(server: LocalServer, app_title: str) -> None:
    _use_crisp_text()
    root = tk.Tk()
    tkfont.nametofont("TkDefaultFont").configure(family="Yu Gothic UI", size=10)
    # pythonw.exe には標準エラーが無い。落ちた理由を黙って捨てないよう、窓で見せる。
    root.report_callback_exception = lambda kind, error, trace: messagebox.showerror(
        "操作パネルでエラーが起きました", f"{kind.__name__}: {error}", parent=root)
    panel = ControlPanel(root, server, app_title)
    panel.start_if_stopped()
    root.mainloop()


def _use_crisp_text() -> None:
    """高解像度の画面で文字がぼやけないようにする。Windows 以外では何もしない。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
