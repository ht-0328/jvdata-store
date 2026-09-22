"""tools/fetch_past_years.py が、期間と種別を正しく fetch に渡すことを固定する。

JV-Link は使わない。``jvstore fetch`` の呼び出しを差し替えて、渡した引数を記録する。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from jvstore.web.db_lock import DatabaseLock

TOOL = Path(__file__).resolve().parents[1] / "tools" / "fetch_past_years.py"
_spec = importlib.util.spec_from_file_location("fetch_past_years", TOOL)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


@pytest.fixture
def calls(monkeypatch):
    """``jvstore fetch`` に渡された引数の記録。RACE だけ失敗させる。"""
    recorded: list[list[str]] = []

    def fake(argv: list[str]) -> int:
        recorded.append(argv)
        return 1 if "RACE" in argv else 0

    monkeypatch.setattr(tool, "jvstore", fake)
    return recorded


def test_開始日は0時から終了日は23時59分59秒までを渡す(tmp_path):
    args = tool.fetch_args("RACE", "20160101", "20221231", tmp_path / "x.duckdb", dry_run=False)
    assert args[args.index("--from") + 1] == "20160101000000"
    assert args[args.index("--to") + 1] == "20221231235959"
    assert args[args.index("--option") + 1] == "4", "古い年はセットアップでしか取れない"
    assert "--dry-run" not in args


def test_終了日を指定できない種別は受け付けない():
    with pytest.raises(Exception, match="DIFN"):
        tool.dataspecs("RACE,DIFN")


def test_失敗した種別があっても残りを続ける(tmp_path, calls, capsys):
    code = tool.main([
        "--from", "20160101", "--to", "20221231",
        "--dataspec", "race,slop", "--db", str(tmp_path / "x.duckdb"),
    ])
    assert code == 1
    assert [argv[argv.index("--dataspec") + 1] for argv in calls] == ["RACE", "SLOP"]
    assert "失敗した種別: RACE" in capsys.readouterr().err


def test_画面が使用中なら取り込まない(tmp_path, calls):
    db = tmp_path / "x.duckdb"
    lock = DatabaseLock(db)
    assert lock.acquire(timeout=1)
    try:
        code = tool.main(["--from", "20160101", "--to", "20221231", "--db", str(db)])
    finally:
        lock.release()
    assert code == 1
    assert calls == []
