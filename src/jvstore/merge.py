"""別の DuckDB に貯めたオッズの断面を、元の DB へ移す。

:mod:`jvstore.timeseries` で別のファイルに貯めた時系列オッズを、元の DB に足すのに使う。
取り込みに数時間かかる間、元の DB を書き込みで開いたままにしないためである。

**元の DB にまだ無い断面だけを足す。** オッズの表（``o1``〜``o6``）の主キーは、レースキーに
``発表月日時分`` を足したもの（:data:`jvstore.store._EXTRA_KEYS`）なので、1つの断面が1行になる。
発表された断面は後から変わらないので、元の DB にすでにある断面は置き換えない。
子の表（``o1__単勝オッズ`` など）は、足した断面のぶんだけ入れる。何度やり直しても行は増えない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .layout import LayoutSet, load_layouts
from .store import DuckStore, TableSpec, _quoted

__all__ = ["ODDS_RECORD_IDS", "MergeResult", "merge_odds"]

#: 移せるオッズのレコード種別。
ODDS_RECORD_IDS = ("O1", "O2", "O3", "O4", "O5", "O6")
_SOURCE = "merge_source"


@dataclass
class MergeResult:
    """表ごとに足した行数。"""

    rows: dict[str, int] = field(default_factory=dict)


def merge_odds(
    source: Path,
    target: Path,
    *,
    record_ids: tuple[str, ...] = ODDS_RECORD_IDS,
    log: Callable[[str], None] = print,
    layouts: LayoutSet | None = None,
) -> MergeResult:
    """``source`` のオッズの断面のうち、``target`` に無いものを ``target`` に足す。"""
    if Path(source).resolve() == Path(target).resolve():
        raise ValueError("移す元と移す先が同じファイルです")
    result = MergeResult()
    store = DuckStore(target, layouts or load_layouts())
    con = store.con
    source_path = Path(source).resolve().as_posix().replace("'", "''")
    con.execute(f"ATTACH '{source_path}' AS {_SOURCE} (READ_ONLY)")
    try:
        con.execute("BEGIN")
        for record_id in record_ids:
            spec = store.specs[record_id]
            if not _source_has(con, spec.table):
                continue
            store.ensure_tables(record_id)
            _merge_table(con, spec, result)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute(f"DETACH {_SOURCE}")
        store.close()
    for table, count in result.rows.items():
        log(f"  {table:<24} {count:>12,} 行を足しました")
    return result


def _merge_table(con, spec: TableSpec, result: MergeResult) -> None:
    """親の表で新しい断面の鍵を決め、その鍵の親と子を足す。"""
    keys = ", ".join(_quoted(key) for key in spec.keys)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE merge_new_keys AS "
        f"SELECT {keys} FROM {_SOURCE}.{_quoted(spec.table)} "
        f"EXCEPT SELECT {keys} FROM {_quoted(spec.table)}"
    )
    _insert(con, spec.table, spec.columns, spec.keys, result)
    for child in spec.children:
        if _source_has(con, child.table):
            _insert(con, child.table, spec.child_columns(child), spec.keys, result)
    con.execute("DROP TABLE merge_new_keys")


def _insert(con, table: str, columns: list[str], keys: list[str], result: MergeResult) -> None:
    listed = ", ".join(_quoted(column) for column in columns)
    selected = ", ".join(f"s.{_quoted(column)}" for column in columns)
    joined = " AND ".join(f"s.{_quoted(key)} = n.{_quoted(key)}" for key in keys)
    count = con.execute(
        f"INSERT INTO {_quoted(table)} ({listed}) SELECT {selected} FROM {_SOURCE}.{_quoted(table)} AS s "
        f"SEMI JOIN merge_new_keys AS n ON {joined}"
    ).fetchone()[0]
    result.rows[table] = result.rows.get(table, 0) + count


def _source_has(con, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = ?", [_SOURCE, table]
        ).fetchone()[0]
    )
