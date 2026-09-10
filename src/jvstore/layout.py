"""JV-Data のレコードレイアウト（＝JV-Data仕様書の「表」）を表すデータモデル。

仕様書 "フォーマット" シートの 1 つの表が 1 つの :class:`RecordLayout` に対応する。
表の中の項目は :class:`Item` で表し、``<登録馬毎情報>`` のような繰返しブロックは
子項目を持つ :class:`Item`（``children``）として保持する。

バイト位置はすべて **0 始まりのバイトオフセット**（仕様書は 1 始まり）に正規化してある。
JV-Data は全角=Shift_JIS 2バイト / 半角=1バイトの固定長なので、
切り出しは必ず ``bytes`` に対して行う（``str`` に decode してから切ると位置がずれる）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

__all__ = ["Item", "RecordLayout", "LayoutSet", "load_layouts"]


@dataclass(slots=True)
class Item:
    """表の 1 行（項目）。繰返しブロックの場合は ``children`` を持つ。"""

    no: str
    """項番。繰返しブロックの子は "37a" のように親項番＋枝番。"""

    name: str
    """項目名（仕様書の表記そのまま。先頭の全角インデントのみ除去）。"""

    offset: int
    """コンテナ先頭からの 0 始まりバイトオフセット。"""

    size: int
    """1 回分のバイト数（繰返しブロックなら 1 ブロック分）。"""

    repeat: int = 1
    """繰返し回数。"""

    is_key: bool = False
    """仕様書の「キー」列に○が付いている項目。"""

    default: str = ""
    """初期値（"0" / "sp" / "Ｓ" など）。"""

    comment: str = ""
    """説明列。"""

    children: list["Item"] = field(default_factory=list)
    """繰返しブロックの内訳。空なら単純項目。"""

    @property
    def is_group(self) -> bool:
        return bool(self.children)

    @property
    def total(self) -> int:
        """この項目が占める合計バイト数。"""
        return self.size * self.repeat

    def to_dict(self) -> dict[str, Any]:
        as_dict: dict[str, Any] = {
            "no": self.no,
            "name": self.name,
            "offset": self.offset,
            "size": self.size,
            "repeat": self.repeat,
            "is_key": self.is_key,
            "default": self.default,
            "comment": self.comment,
        }
        if self.children:
            as_dict["children"] = [child.to_dict() for child in self.children]
        return as_dict

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        return cls(
            no=data["no"],
            name=data["name"],
            offset=data["offset"],
            size=data["size"],
            repeat=data.get("repeat", 1),
            is_key=data.get("is_key", False),
            default=data.get("default", ""),
            comment=data.get("comment", ""),
            children=[cls.from_dict(child) for child in data.get("children", ())],
        )


@dataclass(slots=True)
class RecordLayout:
    """仕様書の 1 つの表（＝1 レコード種別）。"""

    index: str
    """仕様書の表番号（"2", "101" など）。"""

    title: str
    """表題（"レース詳細" など）。"""

    record_id: str
    """レコード種別ID（"RA" など）。"""

    length: int
    """レコード長（バイト、CR/LF を含む）。"""

    items: list[Item] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """出力ファイル名などに使う識別子。例: ``RA_レース詳細``"""
        return f"{self.record_id}_{self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "record_id": self.record_id,
            "length": self.length,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordLayout":
        return cls(
            index=data["index"],
            title=data["title"],
            record_id=data["record_id"],
            length=data["length"],
            items=[Item.from_dict(item) for item in data["items"]],
        )


@dataclass(slots=True)
class DataSpec:
    """JVOpen/JVRTOpen に渡すデータ種別ID と、そこに含まれるレコード種別。"""

    id: str
    name: str
    categories: list[str]
    """"蓄積系" / "速報系" / "セットアップ" のいずれか（複数可）。"""
    record_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "categories": self.categories,
            "record_ids": self.record_ids,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataSpec":
        return cls(
            id=data["id"],
            name=data["name"],
            categories=list(data.get("categories", ())),
            record_ids=list(data.get("record_ids", ())),
        )


@dataclass(slots=True)
class LayoutSet:
    """レコード種別ID → レイアウトの集合と、データ種別IDの一覧。"""

    version: str
    source: str
    layouts: dict[str, RecordLayout]
    dataspecs: dict[str, DataSpec] = field(default_factory=dict)

    def __iter__(self) -> Iterator[RecordLayout]:
        return iter(self.layouts.values())

    def __contains__(self, record_id: str) -> bool:
        return record_id in self.layouts

    def get(self, record_id: str) -> RecordLayout | None:
        return self.layouts.get(record_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "layouts": [layout.to_dict() for layout in self.layouts.values()],
            "dataspecs": [spec.to_dict() for spec in self.dataspecs.values()],
        }

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LayoutSet":
        layouts = [RecordLayout.from_dict(entry) for entry in data["layouts"]]
        specs = [DataSpec.from_dict(entry) for entry in data.get("dataspecs", ())]
        return cls(
            version=data.get("version", ""),
            source=data.get("source", ""),
            layouts={layout.record_id: layout for layout in layouts},
            dataspecs={spec.id: spec for spec in specs},
        )


_DEFAULT_RESOURCE = "layouts.json"


def load_layouts(path: Path | None = None) -> LayoutSet:
    """レイアウト定義を読み込む（既定はパッケージ同梱の layouts.json）。"""
    if path is None:
        return _bundled_layouts()
    return LayoutSet.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@cache
def _bundled_layouts() -> LayoutSet:
    """同梱の layouts.json。38 表ぶんの解析結果を毎回作り直さないよう覚えておく。"""
    resource = resources.files("jvstore.resources").joinpath(_DEFAULT_RESOURCE)
    return LayoutSet.from_dict(json.loads(resource.read_text(encoding="utf-8")))
