"""取得のように時間のかかる処理を別スレッドで動かし、進捗とログを持つ。"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

#: ログの保持行数。1年ぶんの取得は1,300行以上出るので、全部は持たない。
MAX_LOG_LINES = 400
#: 画面に返す行数（末尾のみ）。
TAIL_LINES = 40


def _decode(raw: bytes) -> str:
    """子プロセスの1行を文字列にする。

    UTF-8 は自己検証できるので、まず UTF-8 として読み、通らなければ cp932 と
    みなす。Windows では Python 以外（cmd.exe など）が cp932 で書くことがあり、
    片方に決め打ちすると必ずどちらかが化ける。
    """
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class Task:
    """取得・再判定のように時間のかかる処理の進行状況を持つ。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.label = ""
        self.log: list[str] = []
        self.finished_at: str | None = None
        self.status = "idle"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"running": self.running, "label": self.label,
                    "log": self.log[-TAIL_LINES:], "finished_at": self.finished_at,
                    "status": self.status}

    def start(self, label: str, steps: list[list[str]], cwd: Path,
              db_lock: threading.Lock | None = None) -> bool:
        """処理を別スレッドで始める。すでに動いていれば何もせず False。"""
        if not self._begin(label):
            return False
        threading.Thread(
            target=self._run, args=(steps, cwd, db_lock), daemon=True
        ).start()
        return True

    def _begin(self, label: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.status = "running"
            self.label = label
            self.log = [f"開始: {label}"]
            self.finished_at = None
        return True

    def _run(
        self, steps: list[list[str]], cwd: Path, db_lock: threading.Lock | None
    ) -> None:
        # 子プロセスが DuckDB を書き込みで開くあいだ、リクエスト側が同じ
        # ファイルを開かないよう締め出す。握らずに走らせると、画面の更新と
        # 取り込みが重なった瞬間に取り込みが IO Error で落ちる。
        guard = db_lock if db_lock is not None else contextlib.nullcontext()
        succeeded = False
        try:
            with guard:
                for command in steps:
                    self._append("$ " + " ".join(command))
                    code = self._stream(command, cwd)
                    if code != 0:
                        self._append(f"失敗 (exit {code})")
                        break
                else:
                    succeeded = True
        except Exception:  # noqa: BLE001
            with self.lock:
                self.log.append(traceback.format_exc(limit=2))
        finally:
            self._finish(succeeded)

    def _finish(self, succeeded: bool) -> None:
        with self.lock:
            self.running = False
            self.status = "succeeded" if succeeded else "failed"
            self.finished_at = datetime.now().strftime("%H:%M:%S")
            result = "完了" if succeeded else "失敗"
            self.log.append(f"{result} {self.finished_at}")

    def _append(self, line: str) -> None:
        with self.lock:
            self.log.append(line)
            if len(self.log) > MAX_LOG_LINES:
                del self.log[:-MAX_LOG_LINES]

    def _stream(self, command: list[str], cwd: Path) -> int:
        """子プロセスの出力を1行ずつログへ流す。

        まとめて受け取る（`subprocess.run`）と、過去データの取得のように
        十数分かかる処理のあいだ画面が無反応になる。
        stderr を stdout に混ぜているのは、`jvstore` が「該当データがありません」の
        ような**結果の説明**を stderr に書くため。捨てると0件だったことが分からない。

        出力はバイトのまま受けて自分で解釈する。Windows では、パイプにつないだ
        Python の `sys.stdout.encoding` が既定で cp932 になり、UTF-8 として
        読むと日本語が化けるため。子には `PYTHONIOENCODING` で UTF-8 を
        指定しつつ、それが効かない cmd.exe などの出力に備えて cp932 へ
        フォールバックする。
        """
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env,
        )
        assert process.stdout is not None
        for raw in process.stdout:
            line = _decode(raw).rstrip()
            if line.strip():
                self._append(line)
        return process.wait()


