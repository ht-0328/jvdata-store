"""過去N年ぶんの蓄積系データを、まとめて DuckDB に入れる。

このモジュールが取得の本体で、コマンドラインも画面もここを呼ぶ。
**選ぶのは「何年ぶん」と「どのデータ種別か」だけ**にしてある。
レースや馬で絞り込む機能は持たない。絞り込みは読む側（keiba-yosou）の責務。

データ種別ごとに、初回はセットアップ（option=4）、2回目以降は前回の続き（option=1）。
どこまで取ったかは DuckDB の ``_meta`` に持つので、状態ファイルは要らない。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Sequence

from .layout import LayoutSet, load_layouts
from .store import DuckStore

__all__ = ["SYNC_DATASPECS", "Cancelled", "SyncResult", "check_cancel", "sync"]

#: 過去N年ぶんを取るときに回すデータ種別。この12個で蓄積系の32表を過不足なく覆う。
#:
#: 旧種別（DIFF / BLOD / SNAP / HOSE）ではなく新種別（DIFN / BLDN / SNPN / HOSN）を使う。
#: 新種別は繁殖登録番号・生産者コード・生産者名のサイズ拡張に対応しており、
#: 同梱のレイアウト定義（JV-Data仕様書 4.9.0.1 準拠）と一致するのは新しい方である。
#: 旧種別を取ると UM と BR のバイト位置がずれ、**エラーにならずに壊れた値が入る**。
SYNC_DATASPECS: tuple[tuple[str, str], ...] = (
    ("RACE", "レース情報（レース詳細・馬毎成績・払戻・票数・オッズ）"),
    ("DIFN", "蓄積情報（競走馬・騎手・調教師・生産者・馬主・レコード）"),
    ("BLDN", "血統情報（繁殖馬・産駒・系統）"),
    ("SNPN", "出走時点情報（出走別着度数）"),
    ("MING", "マイニング情報（タイム型・対戦型予想）"),
    ("SLOP", "坂路調教"),
    ("WOOD", "ウッドチップ調教"),
    ("YSCH", "開催スケジュール"),
    ("HOSN", "競走馬市場取引価格"),
    ("HOYU", "馬名の意味由来"),
    ("COMM", "各種解説（コース情報）"),
    ("TOKU", "特別登録馬"),
)

TITLES = dict(SYNC_DATASPECS)


class Cancelled(Exception):
    """利用者が中止した。取得済みのぶんは残す。"""


def check_cancel(stop: Event | None) -> None:
    if stop is not None and stop.is_set():
        raise Cancelled("中止しました。取得済みのデータは次回に再利用します。")


@dataclass
class SyncResult:
    """1回の取得で何が入ったか。"""

    records: int = 0
    failed: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def start_time(years: int, today: "date | None" = None) -> str:
    """`JVOpen` に渡す読み出し開始時刻。

    **年の1月1日まで切り下げる。** JV-Link はセットアップデータを月単位の
    ファイルで持っているので、月の途中を指定しても意味がない。
    JRA-VAN の提供開始は 1986 年なので、それより前は指定しない。
    """
    from datetime import date as _date

    if not 1 <= years <= 40:
        raise ValueError("過去年数は1〜40年で指定してください。")
    year = (today or _date.today()).year - years
    return f"{max(1986, year):04d}0101000000"


def _ready_link(factory):
    """初期化まで済ませた JV-Link を返す。"""
    link = factory()
    link.init()
    return link


def sync(
    db_path: Path,
    years: int = 10,
    dataspecs: Sequence[str] | None = None,
    *,
    log: Callable[[str], None] = print,
    stop: Event | None = None,
    layouts: LayoutSet | None = None,
    #: 初期化まで済ませた JV-Link を返す関数。省略すると自分で用意する。
    link_factory: Callable[[], object] | None = None,
    only: Iterable[str] | None = None,
    dry_run: bool = False,
    force_setup: bool = False,
) -> SyncResult:
    """過去N年ぶんを取り込む。

    途中で失敗したデータ種別があっても、残りは続ける。1種別の不調で
    全部が止まると、数時間かけた取得をやり直すことになる。
    """
    from .jvlink import JVLink, JVLinkError

    layouts = layouts or load_layouts()
    specs = [s.upper() for s in (dataspecs or [d for d, _ in SYNC_DATASPECS])]
    start = start_time(years)
    out = SyncResult()

    log(f"過去 {years} 年（{start[:4]}年1月1日以降）の蓄積系データを取得します")
    log(f"対象データ種別: {', '.join(specs)}")
    log(f"保存先: {db_path.resolve()}")

    store = DuckStore(db_path, layouts, only=only)
    link = link_factory() if link_factory else _ready_link(JVLink)
    try:
        for i, spec in enumerate(specs, 1):
            check_cancel(stop)
            saved = store.meta(f"sync:{spec}")
            fromtime, option = (
                (saved, 1) if saved and not force_setup else (start, 4)
            )
            kind = "続きから" if option == 1 else "セットアップ"
            log("")
            log(f"[{i}/{len(specs)}] {spec} {TITLES.get(spec, '')} — {kind} {fromtime}")
            try:
                result = link.open(spec, fromtime, option)
            except JVLinkError as e:
                log(f"  取得できませんでした: {e}")
                out.failed.append(spec)
                continue
            log(
                f"  対象ファイル {result.read_count:,} 件 / 要ダウンロード "
                f"{result.download_count:,} 件"
            )
            if result.read_count == 0 or dry_run:
                if not dry_run and result.last_file_timestamp:
                    store.set_meta(f"sync:{spec}", result.last_file_timestamp)
                link.close()
                continue
            try:
                _read(link, store, spec, result, log, stop, out)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"  読み込みに失敗しました: {e}")
                out.failed.append(spec)
            finally:
                link.close()
    finally:
        with_error = None
        try:
            link.close()
        except Exception as e:  # noqa: BLE001
            with_error = e
        out.counts = store.counts()
        store.close()
        if with_error is not None:
            log(f"JV-Link を閉じられませんでした: {with_error}")
    return out


def _read(link, store: DuckStore, spec: str, result, log, stop, out: SyncResult) -> None:
    """1データ種別ぶんを読み切って書き込む。"""
    link.wait_download(
        result, on_progress=lambda d, t: log(f"  ダウンロード {d:,}/{t:,}")
    )
    n = 0
    started = time.time()
    for rec in link.records(on_file=lambda f: log(f"  読込: {f}")):
        check_cancel(stop)
        store.write(rec.data)
        n += 1
        if n % 50000 == 0:
            log(f"  {n:,} レコード ({time.time() - started:.0f}秒)")
    store.flush()
    # 読み切ってから記録する。途中で落ちたら次回もう一度同じ範囲を取る。
    if result.last_file_timestamp:
        store.set_meta(f"sync:{spec}", result.last_file_timestamp)
    out.records += n
    log(f"  {n:,} レコード / {time.time() - started:.1f} 秒")
