"""開催日の速報の取得の段取りを、JV-Link なしで固定する。

どのデータ種別をどのキーで取りにいくか、レース毎のキーをどこから拾うか、
「該当データなし」や失敗で全体を止めないことを、偽の JV-Link で確かめる。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from jvstore import load_layouts
from jvstore.jvlink import JVLinkError
from jvstore.realtime import DAY_DATASPECS, RACE_DATASPECS, fetch_day, parse_day

from test_store import RACE, make_record

LAYOUTS = load_layouts()
DAY = RACE["開催年"] + RACE["開催月日"]  # 20260906
RACE_KEY = DAY + RACE["競馬場コード"] + RACE["開催回[第N回]"] + RACE["開催日目[N日目]"] + RACE["レース番号"]


@dataclass
class FakeRecord:
    data: bytes
    filename: str = "0B15.jvd"


class FakeLink:
    """呼ばれた内容を記録し、データ種別ごとに決めたレコードを返す JV-Link。"""

    def __init__(self, records: dict[str, list[bytes]] | None = None, *, broken: tuple[str, ...] = ()):
        self.calls: list[tuple[str, str]] = []
        self.closed = 0
        self._records = records or {}
        self._broken = broken
        self._current: list[bytes] = []

    def rt_open(self, dataspec: str, key: str) -> bool:
        self.calls.append((dataspec, key))
        if dataspec in self._broken:
            raise JVLinkError("JVRTOpen", -303)
        self._current = self._records.get(dataspec, [])
        return bool(self._current)

    def records(self, on_file=None):
        for data in self._current:
            yield FakeRecord(data)

    def close(self):
        self.closed += 1


def _race_record(**over: str) -> bytes:
    return make_record("RA", {**RACE, "データ区分": "2", "データ作成年月日": DAY, **over})


def _fetch(tmp_path, link: FakeLink, day: str = DAY):
    log: list[str] = []
    result = fetch_day(tmp_path / "db.duckdb", day, log=log.append, layouts=LAYOUTS, link_factory=lambda: link)
    return result, log


def test_開催日単位の速報を先に取り_レース毎のオッズはその日のレースのキーで取る(tmp_path):
    local = _race_record(競馬場コード="30")  # 地方のレースには速報オッズが無い
    link = FakeLink({"0B15": [_race_record(), local]})
    result, _ = _fetch(tmp_path, link)
    day_calls = [(dataspec, DAY) for dataspec, _ in DAY_DATASPECS]
    assert link.calls == day_calls + [(dataspec, RACE_KEY) for dataspec, _ in RACE_DATASPECS]
    assert result.races == 1 and result.records["0B15"] == 2 and result.failed == []
    assert link.closed >= len(link.calls), "JVRTOpen のたびに JVClose する"


def test_該当データなしは失敗にしない(tmp_path):
    """前日の馬体重のように、まだ発表されていないデータは空で返る。"""
    link = FakeLink({"0B15": [_race_record()]})
    result, log = _fetch(tmp_path, link)
    assert "0B11" in result.empty and result.failed == []
    assert any("該当データなし" in line for line in log)


def test_1つの種別が失敗しても残りを続ける(tmp_path):
    link = FakeLink({"0B15": [_race_record()]}, broken=("0B14",))
    result, log = _fetch(tmp_path, link)
    assert result.failed == ["0B14"] and ("0B30", RACE_KEY) in link.calls
    assert any("取得できませんでした" in line for line in log)


def test_その日のレースが無ければ_オッズは取りにいかない(tmp_path):
    link = FakeLink()
    result, log = _fetch(tmp_path, link)
    assert result.races == 0 and all(dataspec != "0B30" for dataspec, _ in link.calls)
    assert any("その日のレースが DB にありません" in line for line in log)


def test_取ったレコードは_DuckDB_に入る(tmp_path):
    import duckdb

    odds = make_record("O1", {**RACE, "発表月日時分": "09061000", "データ区分": "1", "データ作成年月日": DAY},
                       repeats={"o1__単勝オッズ": [{"馬番": "01", "オッズ": "0035", "人気順": "01"}]})
    link = FakeLink({"0B15": [_race_record()], "0B30": [odds]})
    _fetch(tmp_path, link)
    con = duckdb.connect(str(tmp_path / "db.duckdb"), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM ra").fetchone()[0] == 1
        assert con.execute('SELECT "馬番", "オッズ", "人気順" FROM "o1__単勝オッズ"').fetchall() == [("01", "0035", "01")]
    finally:
        con.close()


def _win5_record(status: str, made: str, venues: tuple[str, ...] = ("06", "09", "06", "09", "06")) -> bytes:
    """WIN5（WF）の1レコード。対象5レースは、その日の 9〜11R を2場に振り分けた合成の値。"""
    races = [
        {"競馬場コード": venue, "開催回[第N回]": "01", "開催日目[N日目]": "02", "レース番号": f"{9 + index // 2:02d}"}
        for index, venue in enumerate(venues)
    ]
    return make_record(
        "WF",
        {"開催年": DAY[:4], "開催月日": DAY[4:], "データ区分": status, "データ作成年月日": made,
         "キャリーオーバー金額初期": "000000012345678"},
        repeats={"wf__重勝式対象レース情報": races},
    )


def _win5_rows(tmp_path):
    import duckdb

    con = duckdb.connect(str(tmp_path / "db.duckdb"), read_only=True)
    try:
        parent = con.execute('SELECT "開催年", "開催月日", "データ区分", "キャリーオーバー金額初期" FROM wf').fetchall()
        races = con.execute(
            'SELECT "_連番", "競馬場コード", "開催回[第N回]", "開催日目[N日目]", "レース番号" '
            'FROM "wf__重勝式対象レース情報" ORDER BY "_連番"'
        ).fetchall()
    finally:
        con.close()
    return parent, races


def test_WIN5の速報は開催日のキーで取り_wfと対象レースの子表に入る(tmp_path):
    link = FakeLink({"0B15": [_race_record()], "0B51": [_win5_record("1", DAY)]})
    result, _ = _fetch(tmp_path, link)
    assert ("0B51", DAY) in link.calls and result.records["0B51"] == 1
    parent, races = _win5_rows(tmp_path)
    assert parent == [(DAY[:4], DAY[4:], "1", "000000012345678")]
    assert races == [
        (1, "06", "01", "02", "09"), (2, "09", "01", "02", "09"), (3, "06", "01", "02", "10"),
        (4, "09", "01", "02", "10"), (5, "06", "01", "02", "11"),
    ]


def test_WIN5の速報は区分が進むと置き換わり_子表も入れ替わる(tmp_path):
    _fetch(tmp_path, FakeLink({"0B51": [_win5_record("1", DAY)]}))
    _fetch(tmp_path, FakeLink({"0B51": [_win5_record("3", DAY, venues=("05",) * 5)]}))
    parent, races = _win5_rows(tmp_path)
    assert [row[2] for row in parent] == ["3"]
    assert len(races) == 5 and {race[1] for race in races} == {"05"}


def test_蓄積系の確定成績は_あとから取った速報で上書きされない(tmp_path):
    """月曜に作られる確定成績（7）は、日曜に作られた速報（3）より新しい版として残る。"""
    from jvstore.store import DuckStore

    monday = DAY[:6] + f"{int(DAY[6:]) + 1:02d}"
    with DuckStore(tmp_path / "db.duckdb", LAYOUTS) as store:
        store.write(_win5_record("7", monday))
    _fetch(tmp_path, FakeLink({"0B51": [_win5_record("3", DAY, venues=("05",) * 5)]}))
    parent, races = _win5_rows(tmp_path)
    assert [row[2] for row in parent] == ["7"]
    assert {race[1] for race in races} == {"06", "09"}


def test_開催日の書き方():
    assert parse_day("2026-09-20") == "20260920" and parse_day("20260920") == "20260920" and parse_day("2026/09/20") == "20260920"
    for bad in ("", "2026-9-20x", "20261340", "202609"):
        with pytest.raises(ValueError):
            parse_day(bad)
