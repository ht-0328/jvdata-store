"""取り込んだ生テーブルを、表単位で覗くための読み取り口。

予想には使わない。**取ったデータが本当に入っているかを目で確かめる**ためのもの。
件数だけを信じて中身を見ないと、桁落ち・欠損・二重取り込みに気づけない。

表名は必ずデータベースに実在する表と突き合わせてから SQL に埋める。
画面から来た文字列をそのまま識別子にすると、任意のクエリを流し込まれる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

from ..layout import load_layouts

TABLES = {key.lower(): layout.title for key, layout in load_layouts().layouts.items()}

#: 1度に返す列の上限。親テーブルは広くても 500 列ほどだが、
#: 全部返すとブラウザ側が固まる。
MAX_COLUMNS = 60

#: 1度に返す行の上限。
MAX_ROWS = 200

#: 開催日で絞るために要る列。子テーブルも親のキーを持ち回っているので同じ条件で絞れる。
_DATE_COLUMNS = ("開催年", "開催月日")

#: 親テーブルと繰返しブロックの子テーブルを区切る文字列（`o1__単勝オッズ`）。
_BLOCK_SEPARATOR = "__"

#: `YYYYMMDD` の桁数。これ以外の桁ならそのまま返す。
_DATE_LENGTH = 8


@dataclass(frozen=True)
class TableInfo:
    name: str
    record_id: str
    title: str
    rows: int
    """表が持つ全行数。"""
    rows_in_range: int | None
    """指定した期間に入る行数。期間を指定しなかった表・絞れない表は None。"""
    columns: int
    date_from: str | None
    date_to: str | None


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class TableBrowser:
    """1つのウェアハウスを表単位で読む。"""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con

    def names(self) -> list[str]:
        """実在する表の名前。SQL に埋めてよいのはここに載っているものだけ。

        親テーブル（`ra`）と繰返しブロックの子テーブル（`o1__単勝オッズ`）の
        両方が並ぶ。`_meta` のような内部用の表は外す。
        """
        return [
            name
            for (name,) in self.con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE NOT starts_with(table_name, '_') ORDER BY table_name"
            ).fetchall()
        ]

    @staticmethod
    def _title(name: str) -> str:
        """`o1__単勝オッズ` なら「オッズ1（単複枠） › 単勝オッズ」。"""
        parent, _, block = name.partition(_BLOCK_SEPARATOR)
        title = TABLES.get(parent, parent.upper())
        return f"{title} › {block}" if block else title

    @staticmethod
    def _record_id(name: str) -> str:
        return name.partition(_BLOCK_SEPARATOR)[0].upper()

    def _columns(self, name: str) -> list[str]:
        return [
            column
            for (column,) in self.con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE table_name = ? "
                "ORDER BY column_index", [name]
            ).fetchall()
        ]

    def _date_expression(self, columns: list[str]) -> str | None:
        """開催日で絞れる表かどうか。絞れるなら、日付を作る式を返す。

        馬・騎手のマスタのように開催日を持たない表は絞れない。
        """
        if not all(column in columns for column in _DATE_COLUMNS):
            return None
        return '"開催年" || "開催月日"'

    def list_tables(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> list[TableInfo]:
        """表の一覧。期間を渡すと、その期間に入る行数も数える。

        全行数だけを出すと、画面の期間を変えても一覧が動かず、
        「その期間にデータがあるのか」が一覧から読み取れない。
        """
        return [
            self._table_info(name, date_from, date_to) for name in self.names()
        ]

    def _table_info(
        self, name: str, date_from: str | None, date_to: str | None
    ) -> TableInfo:
        columns = self._columns(name)
        rows = self.con.execute(f'SELECT count(*) FROM {_quoted(name)}').fetchone()[0]
        oldest = newest = None
        in_range = None
        date_expression = self._date_expression(columns)
        if date_expression and rows:
            oldest, newest = self.con.execute(
                f'SELECT min({date_expression}), max({date_expression}) '
                f'FROM {_quoted(name)}'
            ).fetchone()
            if date_from or date_to:
                in_range = self._count_in_range(name, columns, date_from, date_to)
        return TableInfo(
            name=name,
            record_id=self._record_id(name),
            title=self._title(name),
            rows=rows,
            rows_in_range=in_range,
            columns=len(columns),
            date_from=_format_date(oldest),
            date_to=_format_date(newest),
        )

    def _count_in_range(
        self,
        name: str,
        columns: list[str],
        date_from: str | None,
        date_to: str | None,
    ) -> int:
        where, params = self._where(columns, date_from, date_to)
        return self.con.execute(
            f'SELECT count(*) FROM {_quoted(name)} {where}', params
        ).fetchone()[0]

    def read(
        self,
        name: str,
        limit: int = 50,
        offset: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
        column_offset: int = 0,
    ) -> dict[str, Any]:
        """1つの表を読む。列も行も上限で切り、切ったことを呼び手に伝える。"""
        if name not in self.names():
            raise LookupError(f"知らない表です: {name}")

        all_columns = self._columns(name)
        limit = max(1, min(int(limit), MAX_ROWS))
        offset = max(0, int(offset))
        column_offset = max(0, min(int(column_offset), max(0, len(all_columns) - 1)))
        columns = all_columns[column_offset:column_offset + MAX_COLUMNS]

        where, params = self._where(all_columns, date_from, date_to)
        total = self.con.execute(
            f'SELECT count(*) FROM {_quoted(name)} {where}', params).fetchone()[0]
        select = ", ".join(_quoted(column) for column in columns)
        rows = self.con.execute(
            f'SELECT {select} FROM {_quoted(name)} {where} LIMIT ? OFFSET ?',
            [*params, limit, offset],
        ).fetchall()
        return {
            "name": name,
            "record_id": self._record_id(name),
            "title": self._title(name),
            "columns": columns,
            "rows": [list(row) for row in rows],
            "total": total,
            "offset": offset,
            "column_total": len(all_columns),
            "column_offset": column_offset,
            "filterable_by_date": self._date_expression(all_columns) is not None,
        }

    def _where(
        self, columns: list[str], date_from: str | None, date_to: str | None
    ) -> tuple[str, list[str]]:
        """開催日で絞る WHERE 句と、その引数。絞れない表なら空。"""
        date_expression = self._date_expression(columns)
        if not date_expression or not (date_from or date_to):
            return "", []
        clauses: list[str] = []
        params: list[str] = []
        if date_from:
            clauses.append(f"{date_expression} >= ?")
            params.append(date_from.replace("-", ""))
        if date_to:
            clauses.append(f"{date_expression} <= ?")
            params.append(date_to.replace("-", ""))
        return "WHERE " + " AND ".join(clauses), params


def _format_date(raw: str | None) -> str | None:
    """'20260301' を '2026-03-01' にする。桁が違えばそのまま返す。"""
    if not raw or len(raw) != _DATE_LENGTH or not raw.isdigit():
        return raw or None
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
