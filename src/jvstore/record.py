"""固定長 JV-Data レコード（bytes）を、表のカラムに展開する。

JV-Data は「全角＝Shift_JIS 2 バイト / 半角＝1 バイト」の固定長レコードなので、
必ず ``bytes`` のまま位置で切り出してから cp932 でデコードする。
先に ``str`` へデコードしてから切ると全角 1 文字＝1 で数えられて位置がずれる。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .layout import Item, RecordLayout

__all__ = [
    "Column",
    "FlatLayout",
    "record_id_of",
    "padded_record",
    "SEPARATOR_NAME",
    "UNKNOWN_STATS_KEY",
]

SEPARATOR_NAME = "レコード区切"
ENCODING = "cp932"

#: レイアウト定義に無いレコード種別の件数を数える、書き出し側の ``stats`` のキー。
UNKNOWN_STATS_KEY = "(未知のレコード種別)"

#: レコード長に足りないぶんを埋める文字。JV-Data の初期値と同じ半角スペース。
_PADDING = b" "


@dataclass(slots=True)
class Column:
    """平坦化された 1 カラム。"""

    name: str
    offset: int
    """レコード先頭からの 0 始まりバイトオフセット。"""
    size: int
    is_key: bool = False
    item_no: str = ""
    comment: str = ""


class FlatLayout:
    """:class:`RecordLayout` を CSV 1 行に対応する形へ平坦化したもの。

    繰返しブロックは ``<登録馬毎情報>_001_馬名`` のように連番付きで展開する。
    切り出し位置は生成時に確定するので、レコードごとの解析はスライスだけになる。
    """

    __slots__ = ("layout", "columns", "keep_separator")

    def __init__(self, layout: RecordLayout, *, keep_separator: bool = False) -> None:
        self.layout = layout
        self.keep_separator = keep_separator
        self.columns = [
            column
            for column in _build_columns(layout)
            if keep_separator or column.name != SEPARATOR_NAME
        ]

    @property
    def record_id(self) -> str:
        return self.layout.record_id

    @property
    def slug(self) -> str:
        return self.layout.slug

    def header(self) -> list[str]:
        return [column.name for column in self.columns]

    def key_columns(self) -> list[str]:
        return [column.name for column in self.columns if column.is_key]

    def parse(self, raw: bytes, *, strip: bool = True) -> list[str]:
        """1 レコード分の bytes をカラム値のリストにする。

        レコード長が足りない場合は半角スペースで埋める（JV-Data の初期値と同じ扱い）。
        """
        raw = padded_record(raw, self.layout.length)
        values = []
        for column in self.columns:
            text = raw[column.offset : column.offset + column.size].decode(
                ENCODING, errors="replace"
            )
            values.append(text.rstrip() if strip else text)
        return values

    def parse_dict(self, raw: bytes, *, strip: bool = True) -> dict[str, str]:
        return dict(zip(self.header(), self.parse(raw, strip=strip)))


def padded_record(raw: bytes, length: int) -> bytes:
    """レコード長に足りないぶんを埋める。JV-Data の初期値と同じ半角スペースを使う。"""
    if len(raw) >= length:
        return raw
    return raw + _PADDING * (length - len(raw))


def _build_columns(layout: RecordLayout) -> list[Column]:
    columns: list[Column] = []
    _collect_columns(layout.items, 0, "", columns)
    return _suffix_duplicates(columns)


def _collect_columns(
    items: Iterable[Item], base: int, prefix: str, out: list[Column]
) -> None:
    """項目を再帰的にたどり、末端の項目だけをカラムとして並べる。"""
    for item in items:
        digits = len(str(item.repeat))
        for index in range(item.repeat):
            offset = base + item.offset + index * item.size
            suffix = "" if item.repeat == 1 else f"_{index + 1:0{digits}d}"
            name = f"{prefix}{item.name}{suffix}"
            if item.children:
                _collect_columns(item.children, offset, f"{name}_", out)
            else:
                out.append(
                    Column(
                        name=name,
                        offset=offset,
                        size=item.size,
                        is_key=item.is_key,
                        item_no=item.no,
                        comment=item.comment,
                    )
                )


def _suffix_duplicates(columns: list[Column]) -> list[Column]:
    """同名カラムの 2 つめ以降に ``#2`` を付ける。仕様書の表内で名前が重複する。"""
    seen: Counter[str] = Counter()
    for column in columns:
        seen[column.name] += 1
        if seen[column.name] > 1:
            column.name = f"{column.name}#{seen[column.name]}"
    return columns


def record_id_of(raw: bytes) -> str:
    """レコード先頭 2 バイトのレコード種別ID。"""
    return raw[:2].decode(ENCODING, errors="replace")
