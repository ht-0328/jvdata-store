"""画面を別プロセスに分けても、読み取りと取得は同時にDBを開かない。"""

import subprocess
import sys

from jvstore.web.db_lock import DatabaseLock


def test_lock_excludes_another_process_and_recovers_after_release(tmp_path):
    db = tmp_path / "shared.duckdb"
    lock = DatabaseLock(db)
    program = """
import sys
from pathlib import Path
from jvstore.web.db_lock import DatabaseLock
lock = DatabaseLock(Path(sys.argv[1]))
acquired = lock.acquire(timeout=0)
print(acquired)
if acquired:
    lock.release()
"""
    with lock:
        result = subprocess.run([sys.executable, "-c", program, str(db)],
                                capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "False"
    result = subprocess.run([sys.executable, "-c", program, str(db)],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "True"
    assert not db.exists()


def test_lock_is_released_on_exception(tmp_path):
    lock = DatabaseLock(tmp_path / "test.duckdb")
    try:
        with lock:
            raise ValueError("failed")
    except ValueError:
        pass
    assert lock.acquire(timeout=0)
    lock.release()
