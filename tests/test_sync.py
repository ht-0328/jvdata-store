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
from jvstore.sync import SYNC_DATASPECS, Cancelled, open_times, start_time, sync

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

    def __init__(self, records=None, read_count=1, timestamps=None):
        self.calls: list[tuple[str, str, int]] = []
        self.closed = 0
        self.inited = 0
        self._records = records or []
        self._read_count = read_count
        #: 開いた読み出し時刻ごとの最新ファイルの時刻。無ければ FakeResult の既定値。
        self._timestamps = timestamps or {}

    def init(self):
        self.inited += 1

    def open(self, dataspec, fromtime, option):
        self.calls.append((dataspec, fromtime, option))
        stamp = self._timestamps.get(fromtime, FakeResult.last_file_timestamp)
        return FakeResult(read_count=self._read_count, last_file_timestamp=stamp)

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
    assert list(dict.fromkeys(c[0] for c in link.calls)) == [d for d, _ in SYNC_DATASPECS]


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
    assert first.calls[0] == ("RACE", start_time(10) + "-" + start_time(9), 4), (
        "初回はセットアップ(option=4)"
    )

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


def test_セットアップは1年ずつに区切り_今年だけ終わりを指定しない():
    """一度に開くファイルが多いと、JV-Link の読み出しが遅くなる。"""
    assert open_times("RACE", "20230101000000", 4, date(2026, 9, 26)) == [
        "20230101000000-20240101000000",
        "20240101000000-20250101000000",
        "20250101000000-20260101000000",
        "20260101000000",
    ]


def test_区切りの終わりは翌年1月1日にする():
    """12月31日にすると、ファイル名の 年月99 と比べて12月分が外れる。"""
    first = open_times("SLOP", "20110101000000", 4, date(2026, 1, 5))[0]
    assert first == "20110101000000-20120101000000"


def test_終わりを指定できない種別は区切らない():
    """DIFN などは終わりを指定すると「該当データなし」になる。"""
    for dataspec in ("DIFN", "HOSN", "HOYU", "COMM", "TOKU"):
        assert open_times(dataspec, "20110101000000", 4, date(2026, 9, 26)) == [
            "20110101000000"
        ]


def test_続きからの取得は区切らない():
    assert open_times("RACE", "20260910120000", 1, date(2026, 9, 26)) == ["20260910120000"]


def test_区切って開いた種別は年の順に全部開く(tmp_path):
    link = FakeLink(read_count=0)
    sync(tmp_path / "db.duckdb", years=3, dataspecs=["RACE", "DIFN"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: link)
    this_year = date.today().year
    assert link.calls == [
        ("RACE", open_time, 4) for open_time in open_times("RACE", start_time(3), 4)
    ] + [("DIFN", start_time(3), 4)]
    assert len(link.calls) == 3 + 1 + 1, f"{this_year - 3}〜{this_year} の4回と DIFN の1回"
    assert link.closed >= len(link.calls), "開くたびに閉じる"


def test_続きの起点は全部の年のうち最も新しい時刻にする(tmp_path):
    """今年の分にファイルが無くても、過去の年の最新時刻から続きを取れるようにする。"""
    db = tmp_path / "db.duckdb"
    times = open_times("RACE", start_time(2), 4)
    stamps = {times[0]: "20250101000000", times[1]: "20260808165606", times[2]: ""}
    sync(db, years=2, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: FakeLink(read_count=0, timestamps=stamps))
    after = FakeLink(read_count=0)
    sync(db, years=2, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: after)
    assert after.calls == [("RACE", "20260808165606", 1)]


def test_途中の年で失敗したら続きの起点を残さない(tmp_path):
    """残すと、失敗した年より後が抜けたまま「取り込み済み」になる。"""
    from jvstore.jvlink import JVLinkError

    times = open_times("RACE", start_time(3), 4)

    class FailSecondYear(FakeLink):
        def open(self, dataspec, fromtime, option):
            if fromtime == times[1]:
                self.calls.append((dataspec, fromtime, option))
                raise JVLinkError("JVOpen", -502)
            return super().open(dataspec, fromtime, option)

    db = tmp_path / "db.duckdb"
    link = FailSecondYear(read_count=0)
    result = sync(db, years=3, dataspecs=["RACE", "DIFN"], log=lambda s: None,
                  layouts=LAYOUTS, link_factory=lambda: link)
    assert result.failed == ["RACE"]
    assert [c[1] for c in link.calls] == [times[0], times[1], start_time(3)], (
        "失敗した年で RACE をやめ、次の種別へ進む"
    )
    again = FakeLink(read_count=0)
    sync(db, years=3, dataspecs=["RACE"], log=lambda s: None,
         layouts=LAYOUTS, link_factory=lambda: again)
    assert again.calls[0] == ("RACE", times[0], 4), "次回は最初の年からセットアップし直す"


def test_受け取ったレコードがテーブルに入る(tmp_path):
    db = tmp_path / "db.duckdb"
    rec = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260910"})
    result = sync(db, years=1, dataspecs=["DIFN"], log=lambda s: None,
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
    assert list(dict.fromkeys(c[0] for c in link.calls)) == ["RACE", "DIFN", "BLDN"]


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
