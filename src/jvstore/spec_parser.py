"""JV-Data仕様書 (xlsx) の「フォーマット」シートからレコードレイアウトを生成する。

SDK 同梱の ``JV-Data仕様書_4.9.0.1.xlsx`` をそのまま読み、表 1 つを 1 レコード種別
として :class:`~jvstore.layout.RecordLayout` に変換する。手打ちのレイアウト定義を持たず
公式仕様書を唯一の出典にすることで、仕様改訂時は xlsx を差し替えて再生成するだけで済む。

シートの列構成（0 始まり）::

    1: 項番 / 表題    2: 枝番(a,b,c…)  3: キー   4: 項目名   5: 位置
    6: 繰返           7: バイト        8: 合計   9: 初期値  10: 説明
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .layout import DataSpec, Item, LayoutSet, RecordLayout

__all__ = ["parse_spec_workbook", "SpecParseError", "validate"]

C_NO, C_SUB, C_KEY, C_NAME, C_POS, C_REPEAT, C_BYTES, C_TOTAL, C_DEFAULT, C_DESC = range(
    1, 11
)

_TITLE_RE = re.compile(r"^([０-９0-9]+)[．.]\s*(.+?)\s*$")
_RELPOS_RE = re.compile(r"^\(\s*(\d+)\s*\)$")
_RECID_RE = re.compile(r'["“]([A-Z0-9]{2})["”]')
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")
_INDENT_CHARS = "　 \t"


class SpecParseError(RuntimeError):
    pass


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _i(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    t = str(v).strip().translate(_ZEN2HAN)
    return int(t) if t.isdigit() else None


def _indent(name_raw: str) -> int:
    return len(name_raw) - len(name_raw.lstrip(_INDENT_CHARS))


def parse_spec_workbook(xlsx: Path, sheet: str = "フォーマット") -> LayoutSet:
    """仕様書 xlsx を読み込んで :class:`LayoutSet` を返す。"""
    import openpyxl  # 生成時のみ必要（実行時依存にはしない）

    xlsx = Path(xlsx)
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        raise SpecParseError(f"シート {sheet!r} が見つかりません: {wb.sheetnames}")
    rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]
    layouts = [_parse_table(rows, s, e) for s, e in _table_ranges(rows)]
    dataspecs = _parse_dataspecs(wb)
    m = re.search(r"(\d+(?:\.\d+)+)", xlsx.name)
    return LayoutSet(
        version=m.group(1) if m else "",
        source=xlsx.name,
        layouts={l.record_id: l for l in layouts},
        dataspecs=dataspecs,
    )


_CATEGORY_RE = re.compile(r"（[１-３1-3]）\s*(蓄積系データ|速報系データ|セットアップデータ)")


def _parse_dataspecs(wb: Any, sheet: str = "データ種別一覧") -> dict[str, DataSpec]:
    """「データ種別一覧」シートから データ種別ID → 含まれるレコード種別 を取り出す。

    列構成: 1=データ種別名 2=データ種別ID 3=フォーマットNo 4=レコード種別名
            5=レコード種別ID 6=収録内容
    データ種別ID セルは "DIFF DIFN" のように複数並ぶことがある。
    """
    if sheet not in wb.sheetnames:
        return {}
    specs: dict[str, DataSpec] = {}
    category = ""
    current: list[str] = []
    for r in wb[sheet].iter_rows(values_only=True):
        cells = [_s(c) for c in r] + [""] * 8
        m = _CATEGORY_RE.search(cells[1])
        if m:
            category = m.group(1).replace("データ", "")
            current = []
            continue
        if cells[1] == "名称" or cells[2].startswith("データ"):
            continue  # 各セクションの見出し行
        ids = cells[2].split()
        if ids:
            current = ids
            for sid in ids:
                spec = specs.setdefault(sid, DataSpec(sid, cells[1], [], []))
                if not spec.name:
                    spec.name = cells[1]
                if category and category not in spec.categories:
                    spec.categories.append(category)
        rec = cells[5]
        if current and re.fullmatch(r"[A-Z0-9]{2}", rec):
            for sid in current:
                rl = specs[sid].record_ids
                if rec not in rl:
                    rl.append(rec)
    return specs


def _table_ranges(rows: list[list[Any]]) -> Iterable[tuple[int, int]]:
    """表題行のインデックスから、各表の [開始行, 終了行) を切り出す。"""
    starts: list[int] = []
    for i, r in enumerate(rows):
        if _is_title_row(r):
            starts.append(i)
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(rows)
        yield s, e


def _is_title_row(r: list[Any]) -> bool:
    if len(r) <= C_BYTES:
        return False
    if not _TITLE_RE.match(_s(r[C_NO])):
        return False
    # 「レコード長 / n / バイト」が同じ行に並ぶものだけを表題とみなす
    return "レコード長" in " ".join(_s(c) for c in r) and _i(r[C_BYTES]) is not None


def _parse_table(rows: list[list[Any]], start: int, end: int) -> RecordLayout:
    title_row = rows[start]
    m = _TITLE_RE.match(_s(title_row[C_NO]))
    assert m is not None
    index = m.group(1).translate(_ZEN2HAN)
    title = m.group(2)
    length = _i(title_row[C_BYTES])
    if length is None:
        raise SpecParseError(f"表 {title}: レコード長を読み取れません")

    items: list[Item] = []
    # 繰返しブロックのスタック（仕様書 4.9 時点で入れ子は無いが将来に備える）
    stack: list[Item] = []
    record_id = ""

    for r in rows[start + 1 : end]:
        if len(r) <= C_DESC:
            r = list(r) + [None] * (C_DESC + 1 - len(r))
        name_raw = "" if r[C_NAME] is None else str(r[C_NAME])
        name = name_raw.strip(_INDENT_CHARS).strip()
        if not name or name == "項目名":
            continue
        size = _i(r[C_BYTES])
        if size is None:
            # バイト数を持たない行＝「<馬場別着回数>」のような見出し行や
            # 「連番002情報」のような説明専用行。レイアウト上の実体はない。
            continue

        no = _s(r[C_NO])
        sub = _s(r[C_SUB])
        is_child = _i(r[C_NO]) is None  # 項番が数字でない行＝繰返しブロックの内訳
        if not is_child:
            stack.clear()

        parent = stack[-1] if (is_child and stack) else None
        container = parent.children if parent is not None else items
        pos_cell = r[C_POS]
        rel = _RELPOS_RE.match(_s(pos_cell))
        pos = int(rel.group(1)) if rel else _i(pos_cell)
        if pos is None:
            # 位置欄が空の行（重勝式の<重勝式対象レース情報>内など）は直前項目から連結
            prev = container[-1] if container else None
            pos = (prev.offset + prev.total + 1) if prev else 1

        item = Item(
            no=(no + sub) if no else ((parent.no if parent else "") + sub),
            name=name.lstrip("<").rstrip(">"),
            offset=pos - 1,
            size=size,
            repeat=_i(r[C_REPEAT]) or 1,
            is_key=_s(r[C_KEY]) in ("○", "◯", "〇"),
            default=_s(r[C_DEFAULT]),
            comment=_s(r[C_DESC]),
        )
        container.append(item)

        if name.startswith("<"):
            stack.append(item)

        if item.name == "レコード種別ID" and not record_id:
            mm = _RECID_RE.search(item.comment)
            if mm:
                record_id = mm.group(1)

    if not record_id:
        raise SpecParseError(f"表 {title}: レコード種別IDを特定できません")
    return RecordLayout(
        index=index, title=title, record_id=record_id, length=length, items=items
    )


def validate(layout: RecordLayout) -> list[str]:
    """レイアウトの自己整合性を検査し、問題点の一覧を返す（空なら健全）。"""
    problems: list[str] = []

    def walk(items: list[Item], container: str, container_size: int) -> None:
        cursor = 0
        for it in items:
            if it.offset != cursor:
                problems.append(
                    f"{container}: {it.no} {it.name} の位置が不連続 "
                    f"(期待 {cursor + 1}, 実際 {it.offset + 1})"
                )
            cursor = it.offset + it.total
            if it.children:
                inner = sum(c.total for c in it.children)
                if inner != it.size:
                    problems.append(
                        f"{container}: <{it.name}> の内訳合計 {inner} が "
                        f"1繰返し分 {it.size} と不一致"
                    )
                walk(it.children, f"{container}/<{it.name}>", it.size)
        if cursor != container_size:
            problems.append(
                f"{container}: 合計 {cursor} バイトがレコード長/ブロック長 "
                f"{container_size} と不一致"
            )

    walk(layout.items, f"{layout.record_id}({layout.title})", layout.length)
    return problems
