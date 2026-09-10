"""画面のサーバーを止める規則。処理（取得など）の途中では止めない。

止める判断はサーバー側だけが持つ。操作パネルやブラウザは結果を伝えるだけにして、
「途中で止めない」という規則を画面ごとに書き分けない。

keiba-yosou にも同じ実装を置き、互いの Python パッケージには依存しない。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

#: 予約中に、処理が終わったかを確かめる間隔（秒）。
RESERVATION_POLL_SECONDS = 1.0


class ClosableTask(Protocol):
    def close_if_idle(self) -> bool: ...


class Shutdown:
    """サーバーの停止と、処理が終わってからの停止の予約。"""

    def __init__(self, task: ClosableTask) -> None:
        self._task = task
        self._lock = threading.Lock()
        self._reserved = False
        # 取り消したあとに予約し直すと、古い見張りと新しい見張りが並ぶ。
        # 世代を数え、古い見張りは自分の世代でなくなった時点で退く。
        self._generation = 0

    @property
    def reserved(self) -> bool:
        with self._lock:
            return self._reserved

    def now(self, stop_server: Callable[[], None]) -> bool:
        """処理が動いていなければ止めて True。動いていれば止めずに False。"""
        if not self._task.close_if_idle():
            return False
        _in_background(stop_server)
        return True

    def after_task(self, stop_server: Callable[[], None]) -> None:
        """処理が終わりしだい止める。すでに予約してあれば何もしない。"""
        with self._lock:
            if self._reserved:
                return
            self._reserved = True
            self._generation += 1
            generation = self._generation
        threading.Thread(
            target=self._stop_when_idle, args=(generation, stop_server), daemon=True
        ).start()

    def cancel(self) -> None:
        """予約を取り消す。予約していなければ何もしない。"""
        with self._lock:
            self._reserved = False
            self._generation += 1

    def _stop_when_idle(self, generation: int, stop_server: Callable[[], None]) -> None:
        while True:
            # 取り消しの確認と締めを同じロックの中で行う。分けると、
            # 取り消した直後に止まることがある。
            with self._lock:
                if self._generation != generation:
                    return
                closed = self._task.close_if_idle()
            if closed:
                stop_server()
                return
            time.sleep(RESERVATION_POLL_SECONDS)


def _in_background(stop_server: Callable[[], None]) -> None:
    # 応答を返し終える前に止めると、呼んだ側に「停止した」が届かない。
    # また http.server の shutdown は待ち受けのスレッドから呼ぶと固まる。
    threading.Thread(target=stop_server, daemon=True).start()
