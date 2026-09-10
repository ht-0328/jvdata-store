"""両アプリの画面で共有する排他規約: <DB絶対パス>.ui.lock の先頭1バイト。

keiba-yosou にも同じ規約の実装を置き、互いの Python パッケージには依存しない。
ロックファイルは残す。プロセス終了時には OS がロックを解放する。
"""

import os
import threading
import time
from pathlib import Path


class DatabaseLock:
    def __init__(self, db: Path):
        self.path = Path(str(db.resolve()) + ".ui.lock")
        self.local = threading.Lock()
        self.file = None

    def acquire(self, timeout: float = 60) -> bool:
        deadline = time.monotonic() + timeout
        if not self.local.acquire(timeout=max(0, timeout)):
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            while True:
                self.file.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except OSError:
                    if time.monotonic() >= deadline:
                        self.file.close()
                        self.file = None
                        self.local.release()
                        return False
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except Exception:
            if self.file is not None:
                self.file.close()
                self.file = None
            self.local.release()
            raise

    def release(self):
        # close は OS のファイルロックも解放する。
        self.file.close()
        self.file = None
        self.local.release()

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("データベースを別の画面で使用中です。処理が終わってから再実行してください。")
        return self

    def __exit__(self, *args):
        self.release()
