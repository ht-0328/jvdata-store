"""DuckDB への登録が、仕様どおり・冪等・新しい版優先になっていることを固定する。

JV-Link は使わない。仕様書のレイアウトから固定長レコードを組み立てて流し込む。
"""

from __future__ import annotations

import pytest

from jvstore import load_layouts
from jvstore.record import ENCODING
from jvstore.store import SEQ_COLUMN, DuckStore, build_spec, build_specs

LAYOUTS = load_layouts()


def make_record(rid: str, values: dict[str, str], *, repeats: dict[str, list[dict[str, str]]] | None = None) -> bytes:
    """レイアウトどおりの1レコードを組み立てる。指定しない項目は半角空白で埋める。"""
    spec = build_spec(LAYOUTS.get(rid))
    buf = bytearray(b" " * spec.length)

    def put(offset: int, size: int, text: str) -> None:
        raw = text.encode(ENCODING)
        if len(raw) > size:
            raise ValueError(f"{text!r} は {size} バイトに収まりません")
        buf[offset : offset + len(raw)] = raw

    put(0, 2, rid)
    for field in spec.fields:
        if field.name in values:
            put(field.offset, field.size, values[field.name])
    for child in spec.children:
        for i, row in enumerate((repeats or {}).get(child.table, [])):
            base = child.base + i * child.stride
            for field in child.fields:
                if field.name in row:
                    put(base + field.offset, field.size, row[field.name])
    return bytes(buf)


RACE = {
    "開催年": "2026",
    "開催月日": "0906",
    "競馬場コード": "06",
    "開催回[第N回]": "01",
    "開催日目[N日目]": "02",
    "レース番号": "11",
}


def store(tmp_path, **kwargs) -> DuckStore:
    return DuckStore(tmp_path / "jvdata.duckdb", LAYOUTS, **kwargs)


# -- スキーマ ---------------------------------------------------------------


def test_繰返しブロックは子テーブルに分かれる():
    spec = build_spec(LAYOUTS.get("H6"))
    assert spec.table == "h6"
    assert len(spec.fields) < 60, "親が仕様書の全列（14,720列）になってはいけない"
    assert [c.table for c in spec.children] == ["h6__3連単票数"]
    child = spec.children[0]
    assert child.repeat == 4896
    assert [f.name for f in child.fields] == ["組番", "票数", "人気順"]


def test_すべての表でスキーマを作れる(tmp_path):
    specs = build_specs(LAYOUTS)
    assert len(specs) == 38
    with store(tmp_path) as s:
        for spec in specs.values():
            s._ensure_table(spec)
        names = set(s.counts())
    assert {"ra", "se", "hr", "o1", "h6__3連単票数", "tk__登録馬毎情報"} <= names


def test_オッズ表は発表時刻が主キーに入る():
    # 時系列オッズを貯めるには、レースキーだけでは断面が1つしか残らない
    assert "発表月日時分" in build_spec(LAYOUTS.get("O1")).keys
    assert "発表月日時分" not in build_spec(LAYOUTS.get("RA")).keys


# -- 書き込み ---------------------------------------------------------------


def test_親と子が両方入る(tmp_path):
    rec = make_record(
        "O5",
        {**RACE, "発表月日時分": "09061530", "データ区分": "1", "データ作成年月日": "20260906"},
        repeats={"o5__3連複オッズ": [
            {"組番": "010203", "オッズ": "001234", "人気順": "001"},
            {"組番": "010204", "オッズ": "005678", "人気順": "002"},
        ]},
    )
    with store(tmp_path) as s:
        s.write(rec)
        s.flush()
        assert s.con.execute("SELECT count(*) FROM o5").fetchone()[0] == 1
        rows = s.con.execute(
            f'SELECT "組番", "オッズ" FROM "o5__3連複オッズ" ORDER BY "{SEQ_COLUMN}"'
        ).fetchall()
    assert rows == [("010203", "001234"), ("010204", "005678")]


def test_空の組は行にしない(tmp_path):
    """3連単は 4,896 枠あるが、埋まっていない枠を行にすると桁違いに膨らむ。"""
    rec = make_record(
        "O5",
        {**RACE, "発表月日時分": "09061530"},
        repeats={"o5__3連複オッズ": [{"組番": "010203", "オッズ": "001234", "人気順": "001"}]},
    )
    with store(tmp_path) as s:
        s.write(rec)
        s.flush()
        assert s.con.execute('SELECT count(*) FROM "o5__3連複オッズ"').fetchone()[0] == 1


def test_桁はそのまま保つ(tmp_path):
    rec = make_record("RA", {**RACE, "データ区分": "7", "距離": "1600"})
    with store(tmp_path) as s:
        s.write(rec)
        s.flush()
        row = s.con.execute('SELECT "競馬場コード", "レース番号", "距離" FROM ra').fetchone()
    assert row == ("06", "11", "1600"), "先頭のゼロやコード値を落としてはいけない"


def test_同じレコードを2回入れても増えない(tmp_path):
    rec = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260907"})
    with store(tmp_path) as s:
        s.write(rec)
        s.flush()
        s.write(rec)
        s.flush()
        assert s.con.execute("SELECT count(*) FROM ra").fetchone()[0] == 1


def test_新しい版が古い版を上書きする(tmp_path):
    old = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260907", "天候コード": "1"})
    new = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260914", "天候コード": "2"})
    with store(tmp_path) as s:
        s.write(old)
        s.flush()
        s.write(new)
        s.flush()
        assert s.con.execute('SELECT "天候コード" FROM ra').fetchone()[0] == "2"


def test_古い版を読み直しても戻らない(tmp_path):
    """取り直しで古いファイルを再送されても、訂正済みの値を壊さない。"""
    old = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260907", "天候コード": "1"})
    new = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260914", "天候コード": "2"})
    with store(tmp_path) as s:
        s.write(new)
        s.flush()
        s.write(old)
        s.flush()
        assert s.con.execute('SELECT "天候コード" FROM ra').fetchone()[0] == "2"


def test_削除レコードは復活しない(tmp_path):
    """データ区分=0（該当レコード削除）は、あとから古い版を読んでも上書きされない。"""
    live = make_record("RA", {**RACE, "データ区分": "7", "データ作成年月日": "20260907"})
    dead = make_record("RA", {**RACE, "データ区分": "0", "データ作成年月日": "20260907"})
    with store(tmp_path) as s:
        s.write(dead)
        s.flush()
        s.write(live)
        s.flush()
        assert s.con.execute('SELECT "データ区分" FROM ra').fetchone()[0] == "0"


def test_頭数が減ったら古い組は消える(tmp_path):
    """出走取消で組合せが減ったとき、前回の組が残っていてはいけない。"""
    def rec(made: str, combos: list[str]) -> bytes:
        return make_record(
            "O5",
            {**RACE, "発表月日時分": "09061530", "データ作成年月日": made},
            repeats={"o5__3連複オッズ": [
                {"組番": c, "オッズ": "001000", "人気順": "001"} for c in combos
            ]},
        )

    with store(tmp_path) as s:
        s.write(rec("20260906", ["010203", "010204", "010205"]))
        s.flush()
        s.write(rec("20260907", ["010203"]))
        s.flush()
        combos = [r[0] for r in s.con.execute('SELECT "組番" FROM "o5__3連複オッズ"').fetchall()]
    assert combos == ["010203"]


def test_同じ取得に訂正が混ざっても主キーが衝突しない(tmp_path):
    """同一ファイル内に旧版と訂正版が並ぶことがある。新しい方だけが残る。"""
    def rec(made: str, odds: str) -> bytes:
        return make_record(
            "O5",
            {**RACE, "発表月日時分": "09061530", "データ作成年月日": made},
            repeats={"o5__3連複オッズ": [{"組番": "010203", "オッズ": odds, "人気順": "001"}]},
        )

    with store(tmp_path) as s:
        s.write(rec("20260906", "001000"))
        s.write(rec("20260907", "002000"))
        s.flush()
        rows = s.con.execute('SELECT "オッズ" FROM "o5__3連複オッズ"').fetchall()
    assert rows == [("002000",)]


def test_時系列オッズは断面ごとに残る(tmp_path):
    """同じレースの複数時刻のオッズが、互いを消さずに並ぶ。"""
    def rec(at: str, odds: str) -> bytes:
        return make_record(
            "O1",
            {**RACE, "発表月日時分": at, "データ作成年月日": "20260906"},
            repeats={"o1__単勝オッズ": [{"馬番": "01", "オッズ": odds, "人気順": "01"}]},
        )

    with store(tmp_path) as s:
        s.write(rec("09061500", "0054"))
        s.write(rec("09061530", "0048"))
        s.flush()
        rows = s.con.execute(
            'SELECT "発表月日時分", "オッズ" FROM "o1__単勝オッズ" ORDER BY "発表月日時分"'
        ).fetchall()
    assert rows == [("09061500", "0054"), ("09061530", "0048")]


def test_only_で表を絞れる(tmp_path):
    with store(tmp_path, only=["RA"]) as s:
        assert s.write(make_record("RA", RACE)) == "RA"
        assert s.write(make_record("SE", {**RACE, "馬番": "01", "血統登録番号": "2020100001"})) is None
        s.flush()
        assert "se" not in s.counts()


def test_未知のレコード種別は数えるだけ(tmp_path):
    with store(tmp_path) as s:
        assert s.write(b"ZZ" + b" " * 100) is None
        assert s.stats["(未知のレコード種別)"] == 1


def test_短いレコードは空白で埋める(tmp_path):
    """レコード長が足りなくても、位置がずれた値を入れずに落ち着かせる。"""
    rec = make_record("RA", {**RACE, "データ区分": "7"})
    with store(tmp_path) as s:
        s.write(rec[:200])
        s.flush()
        assert s.con.execute('SELECT "競馬場コード" FROM ra').fetchone()[0] == "06"


def test_再接続しても続きから書ける(tmp_path):
    rec = make_record("RA", {**RACE, "データ作成年月日": "20260907"})
    with store(tmp_path) as s:
        s.write(rec)
    with store(tmp_path) as s:
        s.write(make_record("RA", {**RACE, "レース番号": "12", "データ作成年月日": "20260907"}))
        s.flush()
        assert s.con.execute("SELECT count(*) FROM ra").fetchone()[0] == 2


def test_メタ情報を持ち回せる(tmp_path):
    with store(tmp_path) as s:
        s.set_meta("sync:RACE", "20260906120000")
    with store(tmp_path) as s:
        assert s.meta("sync:RACE") == "20260906120000"
        assert s.meta("なし", "既定") == "既定"
