"""JV-Data のレコードを DuckDB に登録する。

CSV を経由しない。JV-Link から受け取った固定長レコードを、そのまま
「JV-Data仕様書の表」ごとのテーブルへ書き込む。

**繰返しブロックは子テーブルに分ける。** 仕様書の表をそのまま横に並べると
``H6 票数6（3連単）`` が 14,720 列になり、DuckDB にとっても人にとっても扱えない。
繰返しブロック（``3連単票数`` × 4,896 回など）を縦持ちの子テーブルにすると、
親テーブルは 34 列に収まり、集計も SQL で素直に書ける。

    h6                 開催年 … レース番号 ＋ 発売票数など        （34列）
    h6__3連単票数       開催年 … レース番号 ＋ 連番 ＋ 組番/票数/人気順（10列）

値は**仕様書の桁のままの文字列**で入れる。``競馬場コード='01'``、
``単勝オッズ='0054'``（＝5.4倍）のように、先頭のゼロもコード値も落とさない。
型変換と意味づけは読む側の責務にする。
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from .layout import Item, LayoutSet, RecordLayout
from .record import ENCODING, SEPARATOR_NAME, record_id_of

__all__ = ["DuckStore", "TableSpec", "build_specs", "VERSION_COLUMN", "SEQ_COLUMN"]

#: 更新の新旧を比べるための列。``データ作成年月日`` ＋ ``データ区分`` の順位。
VERSION_COLUMN = "_版"

#: 子テーブルで繰返しの何回目かを表す列。仕様書の列名と衝突しないよう `_` で始める
#: （`TK 登録馬毎情報` は仕様書側に `連番` という列を持っている）。
SEQ_COLUMN = "_連番"

#: ``データ区分`` を新旧比較できる1文字へ写す。
#:
#: 削除(0)と中止(9)を最大にしているのは、あとから古いファイルを読み直しても
#: 削除済みのレコードが**復活しないようにする**ため。地方(A)・海外(B)は
#: 中央の確定成績(7)より前に置き、同じレースで中央の値が勝つようにする。
_STATUS_RANK = {"0": "Z", "9": "Y", "A": "8", "B": "8"}

#: オッズ表は仕様書のキーがレースキーだけになっている。時系列オッズ(0B41/0B42)を
#: 貯めると同じレースに複数の断面が来るため、発表時刻をキーに足す。
#: これがないと最後に取り込んだ断面しか残らない。
_EXTRA_KEYS = {
    "O1": ("発表月日時分",),
    "O2": ("発表月日時分",),
    "O3": ("発表月日時分",),
    "O4": ("発表月日時分",),
    "O5": ("発表月日時分",),
    "O6": ("発表月日時分",),
}


def _ident(name: str) -> str:
    """DuckDB の引用識別子にする。日本語の列名はこれで通る。"""
    return '"' + name.replace('"', '""') + '"'


def _slug(name: str) -> str:
    """テーブル名に使える形へ均す。日本語はそのまま残す。"""
    return re.sub(r"[^0-9A-Za-z぀-ヿ一-鿿]+", "_", name).strip("_")


@dataclass(slots=True)
class Field:
    """テーブルの1列と、レコード内での切り出し位置。"""

    name: str
    offset: int
    size: int


@dataclass(slots=True)
class ChildSpec:
    """繰返しブロック1つぶんの子テーブル。"""

    table: str
    repeat: int
    stride: int
    """1回ぶんのバイト数。``連番`` が1増えるごとに切り出し位置がこれだけ進む。"""
    base: int
    """1回目の先頭オフセット。"""
    fields: list[Field]
    """ブロック先頭からの相対位置で持つ。"""


@dataclass(slots=True)
class TableSpec:
    """1つのレコード種別に対応する親テーブルと子テーブル。"""

    record_id: str
    table: str
    title: str
    length: int
    keys: list[str]
    fields: list[Field]
    children: list[ChildSpec] = field(default_factory=list)

    @property
    def columns(self) -> list[str]:
        return [f.name for f in self.fields] + [VERSION_COLUMN]


def _expand(items: Iterable[Item], base: int, prefix: str, out: list[Field]) -> None:
    """繰返しブロック以外を、連番付きの列として平坦に並べる。"""
    for item in items:
        width = len(str(item.repeat))
        for i in range(item.repeat):
            off = base + item.offset + i * item.size
            suffix = "" if item.repeat == 1 else f"_{i + 1:0{width}d}"
            name = f"{prefix}{item.name}{suffix}"
            if item.children:
                _expand(item.children, off, f"{name}_", out)
            elif item.name != SEPARATOR_NAME:
                out.append(Field(name, off, item.size))


def _dedupe(fields: list[Field]) -> list[Field]:
    seen: Counter[str] = Counter()
    for f in fields:
        seen[f.name] += 1
        if seen[f.name] > 1:
            f.name = f"{f.name}#{seen[f.name]}"
    return fields


def build_spec(layout: RecordLayout) -> TableSpec:
    """仕様書の表1つを、親テーブル＋子テーブルの定義に変換する。"""
    rid = layout.record_id
    parent: list[Field] = []
    children: list[ChildSpec] = []
    for item in layout.items:
        if item.is_group:
            fields: list[Field] = []
            _expand(item.children, 0, "", fields)
            children.append(
                ChildSpec(
                    table=f"{rid.lower()}__{_slug(item.name)}",
                    repeat=item.repeat,
                    stride=item.size,
                    base=item.offset,
                    fields=_dedupe(fields),
                )
            )
        else:
            _expand([item], 0, "", parent)
    parent = _dedupe(parent)

    names = {f.name for f in parent}
    keys = [i.name for i in layout.items if i.is_key and not i.is_group and i.name in names]
    for extra in _EXTRA_KEYS.get(rid, ()):
        if extra in names and extra not in keys:
            keys.append(extra)

    # 子テーブルは「親のキー ＋ 連番 ＋ ブロックの列」で作る。ブロック側に親のキーと
    # 同じ名前の列があると（SE の 1着馬情報 は相手馬の血統登録番号を持つ）テーブルを
    # 作れないので、ブロック側に連番を足して区別する。名前の付け方は仕様書の表内で
    # 重複したときと同じ規則にそろえる。
    reserved = set(keys) | {SEQ_COLUMN}
    for child in children:
        for f in child.fields:
            if f.name in reserved:
                f.name = f"{f.name}#2"

    return TableSpec(
        record_id=rid,
        table=rid.lower(),
        title=layout.title,
        length=layout.length,
        keys=keys,
        fields=parent,
        children=children,
    )


def build_specs(layouts: LayoutSet) -> dict[str, TableSpec]:
    return {rid: build_spec(lay) for rid, lay in layouts.layouts.items()}


class DuckStore:
    """レコードを DuckDB へ書き込むシンク。

    :class:`~jvstore.writer.CsvSink` と同じ ``write(raw)`` / ``close()`` を持つので、
    取得側は出力先を差し替えるだけでよい。

    書き込みは冪等にする。同じレコードを2回入れても行は増えず、
    **より新しい版だけが残る**。新旧は ``データ作成年月日`` と ``データ区分`` で比べる。
    削除レコード（データ区分=0）は行を消さずに残し、古いファイルを読み直しても
    削除が取り消されないようにする。
    """

    def __init__(
        self,
        db_path: Path | str,
        layouts: LayoutSet,
        *,
        only: Iterable[str] | None = None,
        batch: int = 20000,
        strip: bool = True,
    ) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.layouts = layouts
        self.specs = build_specs(layouts)
        self.only = {r.upper() for r in only} if only else None
        self.batch = batch
        self.strip = strip
        self.stats: Counter[str] = Counter()

        self.con = duckdb.connect(str(self.path))
        self.con.execute("PRAGMA enable_progress_bar=false")
        self._ready: set[str] = set()
        self._rows: dict[str, list[tuple]] = {}
        self._pending = 0
        self._ensure_meta()

    # -- スキーマ -----------------------------------------------------------

    def _ensure_meta(self) -> None:
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS _meta(key VARCHAR PRIMARY KEY, value VARCHAR)"
        )
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS _tables("
            "  record_id VARCHAR PRIMARY KEY, table_name VARCHAR, title VARCHAR,"
            "  keys VARCHAR, columns INTEGER, children VARCHAR)"
        )

    def _ensure_table(self, spec: TableSpec) -> None:
        if spec.record_id in self._ready:
            return
        cols = ", ".join(f"{_ident(c)} VARCHAR" for c in spec.columns)
        pk = ", ".join(_ident(k) for k in spec.keys)
        constraint = f", PRIMARY KEY ({pk})" if pk else ""
        self.con.execute(f"CREATE TABLE IF NOT EXISTS {_ident(spec.table)} ({cols}{constraint})")
        for child in spec.children:
            ccols = ", ".join(
                f"{_ident(k)} VARCHAR" for k in spec.keys
            ) + f", {_ident(SEQ_COLUMN)} INTEGER, " + ", ".join(
                f"{_ident(f.name)} VARCHAR" for f in child.fields
            )
            cpk = ", ".join(_ident(k) for k in spec.keys + [SEQ_COLUMN])
            cconstraint = f", PRIMARY KEY ({cpk})" if spec.keys else ""
            self.con.execute(
                f"CREATE TABLE IF NOT EXISTS {_ident(child.table)} ({ccols}{cconstraint})"
            )
        self.con.execute(
            "INSERT OR REPLACE INTO _tables VALUES (?,?,?,?,?,?)",
            (
                spec.record_id,
                spec.table,
                spec.title,
                ",".join(spec.keys),
                len(spec.fields),
                ",".join(c.table for c in spec.children),
            ),
        )
        self._ready.add(spec.record_id)

    # -- 書き込み -----------------------------------------------------------

    def write(self, raw: bytes) -> str | None:
        """1レコードを該当テーブルの投入待ちに積む。書いた種別IDを返す。"""
        rid = record_id_of(raw)
        spec = self.specs.get(rid)
        if spec is None:
            self.stats["(未知のレコード種別)"] += 1
            return None
        if self.only is not None and rid not in self.only:
            return None
        self._ensure_table(spec)

        if len(raw) < spec.length:
            raw = raw + b" " * (spec.length - len(raw))
        cut = self._cut_stripped if self.strip else self._cut_raw

        row = tuple(cut(raw, f.offset, f.size) for f in spec.fields)
        by_name = dict(zip((f.name for f in spec.fields), row))
        version = self._version(by_name)
        self._rows.setdefault(spec.table, []).append(row + (version,))

        key_values = tuple(by_name.get(k, "") for k in spec.keys)
        for child in spec.children:
            rows = self._rows.setdefault(child.table, [])
            for i in range(child.repeat):
                base = child.base + i * child.stride
                values = tuple(cut(raw, base + f.offset, f.size) for f in child.fields)
                if not any(v.strip("0 ") for v in values):
                    continue  # 未使用の枠。3連単の 4,896 組は大半が空になる
                # 版を末尾に付けて運ぶ。同じ取得に訂正が混ざったとき、
                # 子も親と同じ版が勝つようにするために要る（挿入時は落とす）。
                rows.append(key_values + (i + 1,) + values + (version,))

        self.stats[rid] += 1
        self._pending += 1
        if self._pending >= self.batch:
            self.flush()
        return rid

    @staticmethod
    def _cut_stripped(raw: bytes, offset: int, size: int) -> str:
        return raw[offset : offset + size].decode(ENCODING, errors="replace").rstrip()

    @staticmethod
    def _cut_raw(raw: bytes, offset: int, size: int) -> str:
        return raw[offset : offset + size].decode(ENCODING, errors="replace")

    @staticmethod
    def _version(row: dict[str, str]) -> str:
        made = row.get("データ作成年月日", "")
        status = row.get("データ区分", "")
        return made + _STATUS_RANK.get(status, status)

    # -- 反映 ---------------------------------------------------------------

    def flush(self) -> None:
        """溜めた行をテーブルへ反映する。同一キーは新しい版だけ残す。"""
        if not self._pending:
            return
        for spec in self.specs.values():
            rows = self._rows.get(spec.table)
            if not rows:
                continue
            self._merge(spec, rows)
            rows.clear()
        self._pending = 0

    def _merge(self, spec: TableSpec, rows: Sequence[tuple]) -> None:
        """親を当ててから、勝った親の子だけを入れ替える。"""
        table = _ident(spec.table)
        self._insert("tmp_parent", spec.columns, rows)
        if not spec.keys:
            # キーの定義がない表。重複判定ができないので素直に追記する。
            self.con.execute(f"INSERT INTO {table} SELECT * FROM tmp_parent")
            self.con.execute("DROP TABLE tmp_parent")
            self._drop_child_rows(spec)
            return

        keys = ", ".join(_ident(k) for k in spec.keys)
        on = " AND ".join(f"t.{_ident(k)} = s.{_ident(k)}" for k in spec.keys)
        version = _ident(VERSION_COLUMN)
        # 同じ取得の中に同一キーが複数入ることがある（訂正が同じファイルに来る）。
        # キーごとに最新版へ落としてから当てる。
        self.con.execute(
            f"CREATE OR REPLACE TEMP TABLE tmp_latest AS SELECT * FROM tmp_parent "
            f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY {version} DESC) = 1"
        )
        # 既存より新しい（同じ版なら取り直しとみなす）ものだけ置き換える。
        # 置き換えないキーは tmp_won から外し、子テーブルも触らない。
        self.con.execute(
            f"CREATE OR REPLACE TEMP TABLE tmp_won AS SELECT s.* FROM tmp_latest s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {on} "
            f"                  AND t.{version} > s.{version})"
        )
        self.con.execute(f"DELETE FROM {table} t USING tmp_won s WHERE {on}")
        self.con.execute(f"INSERT INTO {table} SELECT * FROM tmp_won")

        for child in spec.children:
            self._merge_child(spec, child)
        self.con.execute("DROP TABLE tmp_parent")
        self.con.execute("DROP TABLE tmp_latest")
        self.con.execute("DROP TABLE tmp_won")

    def _merge_child(self, spec: TableSpec, child: ChildSpec) -> None:
        rows = self._rows.get(child.table)
        table = _ident(child.table)
        parent_on = " AND ".join(f"t.{_ident(k)} = s.{_ident(k)}" for k in spec.keys)
        # 親が入れ替わったら子は丸ごと入れ替える。頭数が減ったときに
        # 前回の組が残るのを防ぐため、消してから入れ直す。
        self.con.execute(f"DELETE FROM {table} t USING tmp_won s WHERE {parent_on}")
        if not rows:
            return
        cols = spec.keys + [SEQ_COLUMN] + [f.name for f in child.fields]
        self._insert("tmp_child", cols + [VERSION_COLUMN], rows, seq_index=len(spec.keys))
        keys = ", ".join(f"c.{_ident(k)}" for k in spec.keys + [SEQ_COLUMN])
        select = ", ".join(f"c.{_ident(c)}" for c in cols)
        join = " AND ".join(f"c.{_ident(k)} = w.{_ident(k)}" for k in spec.keys)
        # 親の勝った版に対応する子だけを入れる。
        self.con.execute(
            f"INSERT INTO {table} SELECT {select} FROM tmp_child c JOIN tmp_won w "
            f"ON {join} AND c.{_ident(VERSION_COLUMN)} = w.{_ident(VERSION_COLUMN)} "
            f"QUALIFY row_number() OVER (PARTITION BY {keys}) = 1"
        )
        self.con.execute("DROP TABLE tmp_child")
        rows.clear()

    def _drop_child_rows(self, spec: TableSpec) -> None:
        for child in spec.children:
            rows = self._rows.get(child.table)
            if rows:
                cols = spec.keys + [SEQ_COLUMN] + [f.name for f in child.fields]
                self._insert("tmp_child", cols + [VERSION_COLUMN], rows, seq_index=len(spec.keys))
                select = ", ".join(_ident(c) for c in cols)
                self.con.execute(
                    f"INSERT INTO {_ident(child.table)} SELECT {select} FROM tmp_child"
                )
                self.con.execute("DROP TABLE tmp_child")
                rows.clear()

    def _insert(
        self,
        name: str,
        columns: Sequence[str],
        rows: Sequence[tuple],
        *,
        seq_index: int | None = None,
    ) -> None:
        """行を一時テーブルへ流し込む。

        **CSV ファイルを経由する。** DuckDB の ``executemany`` は1行ずつ文を実行するため
        桁違いに遅い（実測で 50,000 行に 239 秒。CSV 経由なら 0.10 秒）。
        1レースぶんの3連単オッズだけで数千行になるので、ここが遅いと10年分は終わらない。

        ``allow_quoted_nulls=false`` を付けているのは、**空文字を NULL にしないため**。
        JV-Data の空欄は「値が空」であって「値が無い」ではないので、
        仕様書の桁のままの文字列として保つ。
        """
        fd, tmp = tempfile.mkstemp(suffix=".csv", prefix="jvstore_")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                csv.writer(f, quoting=csv.QUOTE_ALL).writerows(rows)
            source = ", ".join(f"'c{i}': 'VARCHAR'" for i in range(len(columns)))
            select = ", ".join(
                (f"CAST(c{i} AS INTEGER)" if seq_index is not None and i == seq_index else f"c{i}")
                + f" AS {_ident(c)}"
                for i, c in enumerate(columns)
            )
            self.con.execute(
                f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT {select} FROM "
                f"read_csv(?, header=false, allow_quoted_nulls=false, columns={{{source}}})",
                [Path(tmp).as_posix()],
            )
        finally:
            os.unlink(tmp)

    # -- メタ情報 -----------------------------------------------------------

    def meta(self, key: str, default: Any = None) -> Any:
        row = self.con.execute("SELECT value FROM _meta WHERE key = ?", [key]).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO _meta VALUES (?,?)", (key, value))

    def written_files(self) -> list[Path]:
        """書き込み先。取得結果の表示を CsvSink と同じ形にするために持つ。"""
        return [self.path]

    def counts(self) -> dict[str, int]:
        """テーブルごとの行数。取得結果の確認に使う。"""
        out: dict[str, int] = {}
        for (name,) in self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE NOT starts_with(table_name, '_') "
            "ORDER BY table_name"
        ).fetchall():
            out[name] = self.con.execute(f"SELECT count(*) FROM {_ident(name)}").fetchone()[0]
        return out

    def close(self) -> None:
        self.flush()
        self.con.close()

    def __enter__(self) -> "DuckStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
