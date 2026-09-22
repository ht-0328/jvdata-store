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


def value(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_1年ずつに区切り終わりは翌月1日0時にする():
    """``20221231235959`` を終わりにすると、ファイル名が ``20221299…`` の12月分が入らない。"""
    assert tool.yearly_ranges("202203", "202412") == [
        ("20220301000000", "20230101000000"),
        ("20230101000000", "20240101000000"),
        ("20240101000000", "20250101000000"),
    ]


def test_途中の月で終わる期間も翌月1日0時までにする():
    assert tool.yearly_ranges("202001", "202006") == [("20200101000000", "20200701000000")]


def test_セットアップで取り込む(tmp_path):
    argv = tool.fetch_args("RACE", "20160101000000", "20170101000000", tmp_path / "x.duckdb", dry_run=False)
    assert value(argv, "--option") == "4", "古い年はセットアップでしか取れない"
    assert "--dry-run" not in argv


def test_月の形でない指定は受け付けない():
    with pytest.raises(Exception, match="YYYYMM"):
        tool.month("20221231")
    with pytest.raises(Exception, match="YYYYMM"):
        tool.month("202213")


def test_終了時刻を指定できない種別は受け付けない():
    with pytest.raises(Exception, match="DIFN"):
        tool.dataspecs("RACE,DIFN")


def test_失敗した種別があっても残りを続ける(tmp_path, calls, capsys):
    code = tool.main([
        "--from", "202111", "--to", "202202",
        "--dataspec", "race,slop", "--db", str(tmp_path / "x.duckdb"),
    ])
    assert code == 1
    assert [(value(a, "--dataspec"), value(a, "--from"), value(a, "--to")) for a in calls] == [
        ("RACE", "20211101000000", "20220101000000"),
        ("RACE", "20220101000000", "20220301000000"),
        ("SLOP", "20211101000000", "20220101000000"),
        ("SLOP", "20220101000000", "20220301000000"),
    ]
    assert "失敗したもの: RACE 2021年, RACE 2022年" in capsys.readouterr().err


def test_画面が使用中なら取り込まない(tmp_path, calls):
    db = tmp_path / "x.duckdb"
    lock = DatabaseLock(db)
    assert lock.acquire(timeout=1)
    try:
        code = tool.main(["--from", "201601", "--to", "202212", "--db", str(db)])
    finally:
        lock.release()
    assert code == 1
    assert calls == []


def test_前の月と次の月は年をまたぐ():
    assert tool.next_month("202212") == "202301"
    assert tool.previous_month("202301") == "202212"
    assert tool.previous_month("202207") == "202206"
