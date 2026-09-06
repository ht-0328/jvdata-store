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
from .record import FlatLayout, record_id_of

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
        self.only = {r.upper() for r in only} if only else None
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
        rid = record_id_of(raw)
        layout = self.layouts.get(rid)
        if layout is None:
            self.stats["(未知のレコード種別)"] += 1
            self.stats[f"(未知){rid}"] += 1
            self._write_unknown(raw)
            return None
        if self.only is not None and rid not in self.only:
            self.stats[f"{rid}(除外)"] += 1
            return None

        flat = self._flat.get(rid)
        if flat is None:
            flat = FlatLayout(layout, keep_separator=self.keep_separator)
            self._flat[rid] = flat
            self._open(rid, flat)
        self._writers[rid].writerow(flat.parse(raw, strip=self.strip))
        self.stats[rid] += 1
        return rid

    def _open(self, rid: str, flat: FlatLayout) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        path = self.outdir / f"{flat.slug}.csv"
        exists = path.exists() and path.stat().st_size > 0
        mode = "a" if (self.append and exists) else "w"
        f = path.open(mode, encoding=self.encoding, newline="")
        w = csv.writer(f, lineterminator="\n")
        if mode == "w":
            w.writerow(flat.header())
        self._files[rid] = f
        self._writers[rid] = w

    def _write_unknown(self, raw: bytes) -> None:
        if self.unknown_path is None:
            return
        if self._unknown is None:
            self.unknown_path.parent.mkdir(parents=True, exist_ok=True)
            self._unknown = self.unknown_path.open("w", encoding="utf-8", newline="")
        self._unknown.write(raw.decode("cp932", errors="replace").rstrip("\r\n") + "\n")

    def written_files(self) -> list[Path]:
        return [self.outdir / f"{self._flat[r].slug}.csv" for r in sorted(self._flat)]

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()
        self._writers.clear()
        if self._unknown is not None:
            self._unknown.close()
            self._unknown = None

    def __enter__(self) -> "CsvSink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
