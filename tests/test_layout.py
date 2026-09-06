"""同梱レイアウト定義の整合性と、固定長レコードの切り出しを検証する。"""

from __future__ import annotations

import csv

import pytest

from jvstore import CsvSink, FlatLayout, load_layouts, record_id_of
from jvstore.record import SEPARATOR_NAME
from jvstore.spec_parser import validate

LAYOUTS = load_layouts()


def test_全表が読み込める():
    assert len(LAYOUTS.layouts) == 38
    assert {"RA", "SE", "HR", "UM", "WF", "WH"} <= set(LAYOUTS.layouts)


@pytest.mark.parametrize("record_id", sorted(LAYOUTS.layouts))
def test_バイト位置が仕様書のレコード長と一致する(record_id: str):
    layout = LAYOUTS.get(record_id)
    assert validate(layout) == []

    flat = FlatLayout(layout, keep_separator=True)
    # 平坦化しても全カラムが隙間なくレコード長を埋める
    cursor = 0
    for c in flat.columns:
        assert c.offset == cursor, f"{record_id}: {c.name} の位置が不連続"
        cursor += c.size
    assert cursor == layout.length
    assert flat.columns[-1].name == SEPARATOR_NAME


def test_レコード区切は既定で出力しない():
    flat = FlatLayout(LAYOUTS.get("RA"))
    assert SEPARATOR_NAME not in flat.header()


def test_繰返しブロックが連番付きで展開される():
    flat = FlatLayout(LAYOUTS.get("RA"))
    header = flat.header()
    assert header[:5] == [
        "レコード種別ID",
        "データ区分",
        "データ作成年月日",
        "開催年",
        "開催月日",
    ]
    # 本賞金は繰返し7回
    assert [h for h in header if h.startswith("本賞金_")] == [
        f"本賞金_{i}" for i in range(1, 8)
    ]
    # コーナー通過順位は4回繰返しの内訳を持つ
    assert "コーナー通過順位_1_各通過順位" in header
    assert "コーナー通過順位_4_周回数" in header


def _build_record(record_id: str, values: dict[str, str]) -> bytes:
    """テスト用に、指定カラムだけ値を入れた固定長レコードを組み立てる。"""
    layout = LAYOUTS.get(record_id)
    flat = FlatLayout(layout, keep_separator=True)
    buf = bytearray(b" " * layout.length)
    for c in flat.columns:
        if c.name == "レコード種別ID":
            v = record_id.encode("cp932")
        elif c.name in values:
            v = values[c.name].encode("cp932")
        else:
            continue
        assert len(v) <= c.size, f"{c.name} は {c.size} バイトに収まりません"
        buf[c.offset : c.offset + len(v)] = v
    buf[layout.length - 2 :] = b"\r\n"
    return bytes(buf)


def test_全角半角混在レコードをバイト位置で切り出せる():
    raw = _build_record(
        "SE",
        {
            "開催年": "2026",
            "開催月日": "0801",
            "競馬場コード": "01",
            "レース番号": "11",
            "馬番": "07",
            "馬名": "サンライズロイ",  # 全角＝2バイト
            "騎手名略称": "横山武史",
            "確定着順": "01",
        },
    )
    assert len(raw) == LAYOUTS.get("SE").length
    assert record_id_of(raw) == "SE"

    row = FlatLayout(LAYOUTS.get("SE")).parse_dict(raw)
    assert row["開催年"] == "2026"
    assert row["レース番号"] == "11"
    assert row["馬番"] == "07"
    assert row["馬名"] == "サンライズロイ"
    assert row["騎手名略称"] == "横山武史"
    assert row["確定着順"] == "01"


def test_短いレコードは空白で補完される():
    layout = LAYOUTS.get("WE")
    raw = _build_record("WE", {"開催年": "2026"})[:20]
    row = FlatLayout(layout).parse_dict(raw)
    assert row["開催年"] == "2026"
    assert row["レコード種別ID"] == "WE"


def test_CsvSinkが表ごとにファイルを作る(tmp_path):
    raw_se = _build_record("SE", {"開催年": "2026", "馬名": "テストウマ"})
    raw_ra = _build_record("RA", {"開催年": "2026", "競走名本題": "テスト記念"})
    with CsvSink(tmp_path, LAYOUTS) as sink:
        sink.write(raw_se)
        sink.write(raw_ra)
        sink.write(b"ZZ" + b" " * 100)  # 未知のレコード種別

    assert sink.stats["SE"] == 1
    assert sink.stats["RA"] == 1
    assert sink.stats["(未知のレコード種別)"] == 1

    path = tmp_path / "SE_馬毎レース情報.csv"
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["馬名"] == "テストウマ"
    assert (tmp_path / "RA_レース詳細.csv").exists()


def test_データ種別IDの一覧が読み込める():
    race = LAYOUTS.dataspecs["RACE"]
    assert "RA" in race.record_ids and "SE" in race.record_ids
    assert "蓄積系" in race.categories
    assert LAYOUTS.dataspecs["0B12"].categories == ["速報系"]
