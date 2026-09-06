"""jvstore — JRA-VAN Data Lab.(JV-Link) のデータを JV-Data仕様書の表単位で CSV 化する。

主な使い方::

    from jvstore import load_layouts, FlatLayout, JVLink

    layouts = load_layouts()
    flat = FlatLayout(layouts.get("RA"))
    with JVLink() as link:
        link.open("RACE", "20250101000000", 1)
        for rec in link.records():
            if rec.data[:2] == b"RA":
                print(flat.parse_dict(rec.data)["競走名本題"])
"""

from .layout import DataSpec, Item, LayoutSet, RecordLayout, load_layouts
from .record import Column, FlatLayout, record_id_of
from .writer import CsvSink

__all__ = [
    "DataSpec",
    "Item",
    "LayoutSet",
    "RecordLayout",
    "load_layouts",
    "Column",
    "FlatLayout",
    "record_id_of",
    "CsvSink",
    "JVLink",
]


def __getattr__(name: str):  # JV-Link は Windows + COM が必要なので遅延 import
    if name in ("JVLink", "JVLinkError", "OpenResult", "ReadRecord"):
        from . import jvlink

        return getattr(jvlink, name)
    raise AttributeError(name)
