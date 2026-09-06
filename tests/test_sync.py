"""過去N年ぶんの取得の段取りを、JV-Link なしで固定する。

JV-Link は差し替えられる形（`link_factory`）で受け取るので、
偽の JV-Link を渡せば、どの種別をどの option でどこから取りにいくかを確かめられる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from threading import Event

import pytest

from jvstore import load_layouts
from jvstore.store import DuckStore
from jvstore.sync import SYNC_DATASPECS, Cancelled, start_time, sync

from test_store import RACE, make_record

LAYOUTS = load_layouts()


@dataclass
class FakeResult:
    read_count: int = 1
    download_count: int = 0
    last_file_timestamp: str = "20260910120000"


@dataclass
class FakeRecord:
    data: bytes
    filename: str = "RASW.jvd"


class FakeLink:
    """呼ばれた内容を記録するだけの JV-Link。"""

    def __init__(self, records=None, read_count=1):
        self.calls: list[tuple[str, str, int]] = []
        self.closed = 0
        self.inited = 0
        self._records = records or []
        self._read_count = read_count

    def init(self):
        self.inited += 1

    def open(self, dataspec, fromtime, option):
        self.calls.append((dataspec, fromtime, option))
        return FakeResult(read_count=self._read_count)

    def wait_download(self, result, on_progress=None):
        return None

    def records(self, on_file=None):
        for r in self._records:
            yield FakeRecord(r)

    def close(self):
        self.closed += 1


def test_開始時刻は年の1月1日まで切り下げる():
    assert start_time(10, date(2026, 9, 12)) == "20160101000000"
    assert start_time(1, date(2026, 1, 1)) == "20250101000000"


def test_開始時刻は1986年より前にならない():
    """JRA-VAN の提供開始が 1986 年。それより前を指定しても取れるものはない。"""
    assert start_time(40, date(2020, 5, 1)) == "19860101000000"


def test_年数の指定は1から40まで():
    for bad in (0, 41, -1):
        with pytest.raises(ValueError):
            start_time(bad, date(2026, 1, 1))


def test_既定で蓄積系の12種別を回す(tmp_path):
    link = FakeLink(read_count=0)
    sync(tmp_path / "db.duckdb", years=10, log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: link)
    assert [c[0] for c in link.calls] == [d for d, _ in SYNC_DATASPECS]


def test_旧種別ではなく新種別を使う():
    """旧種別は UM と BR のバイト位置がずれ、エラーにならずに壊れた値が入る。"""
    specs = {d for d, _ in SYNC_DATASPECS}
    assert {"DIFN", "BLDN", "SNPN", "HOSN"} <= specs
    assert not ({"DIFF", "BLOD", "SNAP", "HOSE"} & specs)


def test_初回はセットアップ_2回目は続きから(tmp_path):
    db = tmp_path / "db.duckdb"
    first = FakeLink(read_count=0)
    sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: first)
    assert first.calls == [("RACE", start_time(10), 4)], "初回はセットアップ(option=4)"

    second = FakeLink(read_count=0)
    sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: second)
    assert second.calls == [("RACE", "20260910120000", 1)], "2回目は前回の続き(option=1)"


def test_force_setup_でセットアップからやり直せる(tmp_path):
    db = tmp_path / "db.duckdb"
    sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: FakeLink(read_count=0))
    again = FakeLink(read_count=0)
    sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: again, force_setup=True)
    assert again.calls[0][2] == 4


def test_受け取ったレコードがテーブルに入る(tmp_path):
    db = tmp_path / "db.duckdb"
    rec = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260910"})
    result = sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
                  layouts=LAYOUTS, link_factory=lambda: FakeLink([rec]))
    assert result.records == 1
    assert result.counts["ra"] == 1


def test_dry_run_では書き込まない(tmp_path):
    db = tmp_path / "db.duckdb"
    rec = make_record("RA", RACE)
    result = sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
                  layouts=LAYOUTS, link_factory=lambda: FakeLink([rec]), dry_run=True)
    assert result.records == 0
    assert result.counts.get("ra", 0) == 0


def test_dry_run_は取得位置を進めない(tmp_path):
    """件数を見ただけで「取り込み済み」にすると、本番の取得が飛んでしまう。"""
    db = tmp_path / "db.duckdb"
    sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: FakeLink(read_count=5), dry_run=True)
    after = FakeLink(read_count=0)
    sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: after)
    assert after.calls[0][2] == 4, "dry-run のあともセットアップから取る"


def test_1種別が失敗しても残りは続ける(tmp_path):
    from jvstore.jvlink import JVLinkError

    class Flaky(FakeLink):
        def open(self, dataspec, fromtime, option):
            self.calls.append((dataspec, fromtime, option))
            if dataspec == "RACE":
                raise JVLinkError("JVOpen", -1)
            return FakeResult(read_count=0)

    link = Flaky()
    result = sync(tmp_path / "db.duckdb", years=10, dataspecs=["RACE", "DIFN", "BLDN"],
                  log=lambda s: None, layouts=LAYOUTS, link_factory=lambda: link)
    assert result.failed == ["RACE"]
    assert [c[0] for c in link.calls] == ["RACE", "DIFN", "BLDN"]


def test_中止できる(tmp_path):
    stop = Event()
    stop.set()
    with pytest.raises(Cancelled):
        sync(tmp_path / "db.duckdb", years=10, dataspecs=["RACE"],
             log=lambda s: None, layouts=LAYOUTS,
             link_factory=lambda: FakeLink(read_count=0), stop=stop)


def test_中止しても取得済みは残る(tmp_path):
    """数時間かけた取得が、中止でまるごと消えては困る。"""
    db = tmp_path / "db.duckdb"
    rec = make_record("RA", {**RACE, "データ作成年月日": "20260910"})
    sync(db, years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: FakeLink([rec]))
    stop = Event()
    stop.set()
    with pytest.raises(Cancelled):
        sync(db, years=10, dataspecs=["DIFN"], log=lambda s: None,
             layouts=LAYOUTS, link_factory=lambda: FakeLink(read_count=0), stop=stop)
    with DuckStore(db, LAYOUTS) as store:
        assert store.counts()["ra"] == 1


def test_JV_Link_は必ず閉じる(tmp_path):
    link = FakeLink(read_count=0)
    sync(tmp_path / "db.duckdb", years=10, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: link)
    assert link.closed >= 1
