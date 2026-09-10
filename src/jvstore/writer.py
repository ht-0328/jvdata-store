"""レコードを「JV-Data仕様書の表」単位で CSV へ振り分けて書き出す。

1 レコード種別 = 1 CSV。ファイル名は ``RA_レース詳細.csv`` のように
レコード種別ID＋表題。ヘッダは仕様書の項目名（繰返しは連番付き）。
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import IO, Iterable

from .layout import LayoutSet
from .record import ENCODING, UNKNOWN_STATS_KEY, FlatLayout, record_id_of

__all__ = ["CsvSink"]


class CsvSink:
    """レコード種別ごとに CSV ファイルを開き、書き分けるシンク。"""

    def __init__(
        self,
        outdir: Path,
        layouts: LayoutSet,
        *,
        encoding: str = "utf-8-sig",
        strip: bool = True,
        keep_separator: bool = False,
        only: Iterable[str] | None = None,
        append: bool = False,
        unknown_path: Path | None = None,
    ) -> None:
        self.outdir = Path(outdir)
        self.layouts = layouts
        self.encoding = encoding
        self.strip = strip
        self.keep_separator = keep_separator
        self.only = {record_id.upper() for record_id in only} if only else None
        self.append = append
        self.unknown_path = unknown_path
        self.stats: Counter[str] = Counter()

        self._flat: dict[str, FlatLayout] = {}
        self._files: dict[str, IO[str]] = {}
        self._writers: dict[str, "csv._writer"] = {}
        self._unknown: IO[str] | None = None

    # ------------------------------------------------------------------
    def write(self, raw: bytes) -> str | None:
        """1 レコードを該当する CSV に書く。書いたレコード種別IDを返す。"""
        record_id = record_id_of(raw)
        layout = self.layouts.get(record_id)
        if layout is None:
            self.stats[UNKNOWN_STATS_KEY] += 1
            self.stats[f"(未知){record_id}"] += 1
            self._write_unknown(raw)
            return None
        if self._is_excluded(record_id):
            self.stats[f"{record_id}(除外)"] += 1
            return None

        flat = self._flat.get(record_id)
        if flat is None:
            flat = FlatLayout(layout, keep_separator=self.keep_separator)
            self._flat[record_id] = flat
            self._open(record_id, flat)
        self._writers[record_id].writerow(flat.parse(raw, strip=self.strip))
        self.stats[record_id] += 1
        return record_id

    def _is_excluded(self, record_id: str) -> bool:
        return self.only is not None and record_id not in self.only

    def _open(self, record_id: str, flat: FlatLayout) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        path = self.outdir / f"{flat.slug}.csv"
        has_content = path.exists() and path.stat().st_size > 0
        appending = self.append and has_content
        stream = path.open("a" if appending else "w", encoding=self.encoding, newline="")
        writer = csv.writer(stream, lineterminator="\n")
        if not appending:
            writer.writerow(flat.header())
        self._files[record_id] = stream
        self._writers[record_id] = writer

    def _write_unknown(self, raw: bytes) -> None:
        if self.unknown_path is None:
            return
        if self._unknown is None:
            self.unknown_path.parent.mkdir(parents=True, exist_ok=True)
            self._unknown = self.unknown_path.open("w", encoding="utf-8", newline="")
        self._unknown.write(raw.decode(ENCODING, errors="replace").rstrip("\r\n") + "\n")

    def written_files(self) -> list[Path]:
        return [
            self.outdir / f"{self._flat[record_id].slug}.csv"
            for record_id in sorted(self._flat)
        ]

    def close(self) -> None:
        for stream in self._files.values():
            stream.close()
        self._files.clear()
        self._writers.clear()
        if self._unknown is not None:
            self._unknown.close()
            self._unknown = None

    def __enter__(self) -> "CsvSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
