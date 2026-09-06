"""固定長 JV-Data レコード（bytes）を、表のカラムに展開する。

JV-Data は「全角＝Shift_JIS 2 バイト / 半角＝1 バイト」の固定長レコードなので、
必ず ``bytes`` のまま位置で切り出してから cp932 でデコードする。
先に ``str`` へデコードしてから切ると全角 1 文字＝1 で数えられて位置がずれる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .layout import Item, RecordLayout

__all__ = ["Column", "FlatLayout", "record_id_of", "SEPARATOR_NAME"]

SEPARATOR_NAME = "レコード区切"
ENCODING = "cp932"


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
            c
            for c in _build_columns(layout)
            if keep_separator or c.name != SEPARATOR_NAME
        ]

    @property
    def record_id(self) -> str:
        return self.layout.record_id

    @property
    def slug(self) -> str:
        return self.layout.slug

    def header(self) -> list[str]:
        return [c.name for c in self.columns]

    def key_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.is_key]

    def parse(self, raw: bytes, *, strip: bool = True) -> list[str]:
        """1 レコード分の bytes をカラム値のリストにする。

        レコード長が足りない場合は半角スペースで埋める（JV-Data の初期値と同じ扱い）。
        """
        need = self.layout.length
        if len(raw) < need:
            raw = raw + b" " * (need - len(raw))
        out = []
        for c in self.columns:
            v = raw[c.offset : c.offset + c.size].decode(ENCODING, errors="replace")
            out.append(v.rstrip() if strip else v)
        return out

    def parse_dict(self, raw: bytes, *, strip: bool = True) -> dict[str, str]:
        return dict(zip(self.header(), self.parse(raw, strip=strip)))


def _build_columns(layout: RecordLayout) -> list[Column]:
    cols: list[Column] = []
    _walk(layout.items, 0, "", cols)
    return _dedupe(cols)


def _walk(items: Iterable[Item], base: int, prefix: str, out: list[Column]) -> None:
    for item in items:
        width = len(str(item.repeat))
        for i in range(item.repeat):
            off = base + item.offset + i * item.size
            suffix = "" if item.repeat == 1 else f"_{i + 1:0{width}d}"
            name = f"{prefix}{item.name}{suffix}"
            if item.children:
                _walk(item.children, off, f"{name}_", out)
            else:
                out.append(
                    Column(
                        name=name,
                        offset=off,
                        size=item.size,
                        is_key=item.is_key,
                        item_no=item.no,
                        comment=item.comment,
                    )
                )


def _dedupe(cols: list[Column]) -> list[Column]:
    seen: dict[str, int] = {}
    for c in cols:
        n = seen.get(c.name, 0) + 1
        seen[c.name] = n
        if n > 1:
            c.name = f"{c.name}#{n}"
    return cols


def record_id_of(raw: bytes) -> str:
    """レコード先頭 2 バイトのレコード種別ID。"""
    return raw[:2].decode(ENCODING, errors="replace")
