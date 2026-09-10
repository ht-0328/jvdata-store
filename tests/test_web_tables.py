"""表ブラウザの安全側を固定する。

画面から来た文字列で SQL の識別子を組み立てるので、
「知らない名前は SQL に触れる前に落ちる」ことをテストで守る。
"""

from __future__ import annotations

import duckdb
import pytest

from jvstore.web.tables import MAX_COLUMNS, TableBrowser


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE ra AS
        SELECT * FROM (VALUES
            ('2026', '0301', '01'),
            ('2026', '0815', '05'),
            ('2025', '1201', '06')
        ) t("開催年", "開催月日", "競馬場コード")
    """)
    yield c
    c.close()


def test_知らない表名は_SQL_に触れる前に落ちる(con):
    with pytest.raises(LookupError):
        TableBrowser(con).read('ra" ; DROP TABLE ra; --')


def test_取り込まれていない表は落ちる(con):
    with pytest.raises(LookupError):
        TableBrowser(con).read("se")


def test_一覧は開催日の範囲を返す(con):
    [ra] = TableBrowser(con).list_tables()
    assert ra.record_id == "RA"
    assert ra.rows == 3
    assert (ra.date_from, ra.date_to) == ("2025-12-01", "2026-08-15")
    assert ra.rows_in_range is None          # 期間を渡していない


def test_期間を渡すとその期間の行数を数える(con):
    [ra] = TableBrowser(con).list_tables("2026-01-01", "2026-12-31")
    assert ra.rows_in_range == 2
    assert ra.rows == 3                      # 全行数は変わらない


def test_期間で絞れる(con):
    d = TableBrowser(con).read("ra", date_from="2026-04-01", date_to="2026-12-31")
    assert d["total"] == 1
    assert d["rows"][0][:2] == ["2026", "0815"]
    assert d["filterable_by_date"] is True


def test_行数の上限を超えて返さない(con):
    d = TableBrowser(con).read("ra", limit=10_000)
    assert len(d["rows"]) <= 3               # 元データが3行


def test_列は上限ずつ返し_全体の列数も伝える(con):
    d = TableBrowser(con).read("ra")
    assert len(d["columns"]) <= MAX_COLUMNS
    assert d["column_total"] == 3
    assert d["column_offset"] == 0


def test_列送りができる(con):
    d = TableBrowser(con).read("ra", column_offset=2)
    assert d["columns"] == ["競馬場コード"]
    assert d["column_offset"] == 2
