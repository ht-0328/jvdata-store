"""時系列オッズのまとめ取りと、別の DB からの移し替えを、JV-Link なしで固定する。

期間のレースをどこから拾い、どのデータ種別・キーで取りにいくか、取り込み済みのレースを飛ばして
続きから取れること、移し替えで断面が重ならないことを、偽の JV-Link と合成レコードで確かめる。
"""

from __future__ import annotations

import duckdb
import pytest

from jvstore import load_layouts
from jvstore.merge import merge_odds
from jvstore.store import DuckStore
from jvstore.timeseries import TIMESERIES_DATASPECS, fetch_timeseries

from test_realtime import FakeLink
from test_store import RACE, make_record

LAYOUTS = load_layouts()
DAY = RACE["開催年"] + RACE["開催月日"]  # 20260906
RACE_KEY = DAY + RACE["競馬場コード"] + RACE["開催回[第N回]"] + RACE["開催日目[N日目]"] + RACE["レース番号"]


def _races_db(tmp_path, *races: dict[str, str]):
    path = tmp_path / "races.duckdb"
    with DuckStore(path, LAYOUTS) as store:
        for race in races:
            store.write(make_record("RA", {**race, "データ区分": "7", "データ作成年月日": DAY}))
    return path


def _win_odds(announced: str, stage: str = "1", odds: str = "0035") -> bytes:
    return make_record(
        "O1", {**RACE, "発表月日時分": announced, "データ区分": stage, "データ作成年月日": DAY},
        repeats={"o1__単勝オッズ": [{"馬番": "01", "オッズ": odds, "人気順": "01"}]},
    )


def _quinella_odds(announced: str) -> bytes:
    return make_record(
        "O2", {**RACE, "発表月日時分": announced, "データ区分": "1", "データ作成年月日": DAY},
        repeats={"o2__馬連オッズ": [{"組番": "0102", "オッズ": "000123", "人気順": "001"}]},
    )


def _fetch(tmp_path, link: FakeLink, races_db, *, first=DAY, last=DAY, **kwargs):
    log: list[str] = []
    result = fetch_timeseries(
        tmp_path / "timeseries.duckdb", first, last, races_db=races_db, log=log.append, layouts=LAYOUTS,
        link_factory=lambda: link, **kwargs,
    )
    return result, log


def _rows(path, sql: str):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_期間の中央のレースだけを_単複枠と馬連の時系列で取りにいく(tmp_path):
    races_db = _races_db(
        tmp_path, RACE, {**RACE, "競馬場コード": "30"}, {**RACE, "開催月日": "0913"},
    )
    link = FakeLink({"0B41": [_win_odds("09061000")], "0B42": [_quinella_odds("09061000")]})
    result, _ = _fetch(tmp_path, link, races_db)
    assert link.calls == [(dataspec, RACE_KEY) for dataspec, _, _ in TIMESERIES_DATASPECS], "地方と期間の外は取らない"
    assert result.races == 1 and result.records == {"0B41": 1, "0B42": 1} and result.failed == []


def test_断面は発表時刻ごとに別の行で残る(tmp_path):
    races_db = _races_db(tmp_path, RACE)
    link = FakeLink({"0B41": [_win_odds("09061000"), _win_odds("09061005", odds="0040"), _win_odds("09061010", "3")]})
    _fetch(tmp_path, link, races_db)
    rows = _rows(tmp_path / "timeseries.duckdb", 'SELECT "発表月日時分", "オッズ" FROM "o1__単勝オッズ" ORDER BY 1')
    assert rows == [("09061000", "0035"), ("09061005", "0040"), ("09061010", "0035")]


def test_取り込み済みのレースは飛ばして続きから取れる(tmp_path):
    races_db = _races_db(tmp_path, RACE)
    _fetch(tmp_path, FakeLink({"0B41": [_win_odds("09061000")]}), races_db)
    link = FakeLink({"0B41": [_win_odds("09061000")], "0B42": [_quinella_odds("09061000")]})
    result, _ = _fetch(tmp_path, link, races_db)
    assert link.calls == [("0B42", RACE_KEY)], "単複枠は取り込み済み、馬連はまだ"
    assert result.skipped == {"0B41": 1}


def test_取り直しを指定すると取り込み済みでも取る(tmp_path):
    races_db = _races_db(tmp_path, RACE)
    _fetch(tmp_path, FakeLink({"0B41": [_win_odds("09061000")]}), races_db)
    link = FakeLink({"0B41": [_win_odds("09061000")]})
    _fetch(tmp_path, link, races_db, refetch=True)
    assert ("0B41", RACE_KEY) in link.calls


def test_該当データなしと失敗で止めない(tmp_path):
    races_db = _races_db(tmp_path, RACE, {**RACE, "レース番号": "12"})
    link = FakeLink({"0B42": [_quinella_odds("09061000")]}, broken=("0B41",))
    result, _ = _fetch(tmp_path, link, races_db)
    assert len(link.calls) == 4, "1レース目の失敗のあとも2レース目を取りにいく"
    assert len(result.failed) == 2 and result.records == {"0B41": 0, "0B42": 2}


def test_開始日が終了日より後なら止める(tmp_path):
    with pytest.raises(ValueError):
        _fetch(tmp_path, FakeLink(), _races_db(tmp_path, RACE), first="20260907", last="20260906")


def test_移し替えは元の_DB_に無い断面だけを足す(tmp_path):
    source, target = tmp_path / "timeseries.duckdb", tmp_path / "jvdata.duckdb"
    with DuckStore(source, LAYOUTS) as store:
        for data in (_win_odds("09061000"), _win_odds("09061005", odds="0040"), _quinella_odds("09061000")):
            store.write(data)
    with DuckStore(target, LAYOUTS) as store:
        store.write(_win_odds("09061000"))  # すでにある断面
        store.write(_win_odds("00000000", "5"))  # 確定オッズ
    merge_odds(source, target, log=lambda _: None, layouts=LAYOUTS)
    merge_odds(source, target, log=lambda _: None, layouts=LAYOUTS)  # やり直しても増えない
    assert _rows(target, 'SELECT "発表月日時分" FROM o1 ORDER BY 1') == [("00000000",), ("09061000",), ("09061005",)]
    assert _rows(target, 'SELECT "発表月日時分", "オッズ" FROM "o1__単勝オッズ" ORDER BY 1') == [
        ("00000000", "0035"), ("09061000", "0035"), ("09061005", "0040"),
    ]
    assert _rows(target, 'SELECT count(*) FROM "o2__馬連オッズ"') == [(1,)], "元の DB に無い表も作って足す"


def test_移す元と移す先が同じなら止める(tmp_path):
    path = tmp_path / "jvdata.duckdb"
    DuckStore(path, LAYOUTS).close()
    with pytest.raises(ValueError):
        merge_odds(path, path, log=lambda _: None, layouts=LAYOUTS)
