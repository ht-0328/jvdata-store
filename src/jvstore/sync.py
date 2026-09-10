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
from datetime import date
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

from .layout import LayoutSet, load_layouts
from .store import DuckStore

if TYPE_CHECKING:  # JV-Link は Windows + COM が必要なので実行時には読み込まない
    from .jvlink import JVLink, OpenResult

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

#: ``_meta`` に前回の続きの時刻を残すときのキーの接頭辞。
_META_PREFIX = "sync:"

#: JVOpen の option。1:通常（前回の続き） 4:ダイアログ無しセットアップ。
_OPTION_CONTINUE = 1
_OPTION_SETUP = 4

#: JRA-VAN の提供開始年。これより前は指定しない。
_FIRST_YEAR = 1986

#: 過去年数として受け付ける範囲（両端を含む）。
_MIN_YEARS = 1
_MAX_YEARS = 40

#: 途中経過を出すレコード件数の刻み。
_PROGRESS_EVERY = 50000


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


def start_time(years: int, today: date | None = None) -> str:
    """`JVOpen` に渡す読み出し開始時刻。

    **年の1月1日まで切り下げる。** JV-Link はセットアップデータを月単位の
    ファイルで持っているので、月の途中を指定しても意味がない。
    JRA-VAN の提供開始は 1986 年なので、それより前は指定しない。
    """
    if not _MIN_YEARS <= years <= _MAX_YEARS:
        raise ValueError(f"過去年数は{_MIN_YEARS}〜{_MAX_YEARS}年で指定してください。")
    year = (today or date.today()).year - years
    return f"{max(_FIRST_YEAR, year):04d}0101000000"


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
    from .jvlink import JVLink

    layouts = layouts or load_layouts()
    specs = [name.upper() for name in (dataspecs or [name for name, _ in SYNC_DATASPECS])]
    start = start_time(years)
    summary = SyncResult()

    log(f"過去 {years} 年（{start[:4]}年1月1日以降）の蓄積系データを取得します")
    log(f"対象データ種別: {', '.join(specs)}")
    log(f"保存先: {db_path.resolve()}")

    store = DuckStore(db_path, layouts, only=only)
    link = link_factory() if link_factory else _initialised_link(JVLink)
    try:
        for position, dataspec in enumerate(specs, 1):
            check_cancel(stop)
            fromtime, option = _resume_point(store, dataspec, start, force_setup)
            kind = "続きから" if option == _OPTION_CONTINUE else "セットアップ"
            log("")
            log(f"[{position}/{len(specs)}] {dataspec} {TITLES.get(dataspec, '')}"
                f" — {kind} {fromtime}")
            _sync_dataspec(
                link, store, dataspec, fromtime, option, summary,
                log=log, stop=stop, dry_run=dry_run,
            )
    finally:
        close_error = None
        try:
            link.close()
        except Exception as error:  # noqa: BLE001
            close_error = error
        summary.counts = store.counts()
        store.close()
        if close_error is not None:
            log(f"JV-Link を閉じられませんでした: {close_error}")
    return summary


def _initialised_link(factory: Callable[[], Any]) -> Any:
    """初期化まで済ませた JV-Link を返す。"""
    link = factory()
    link.init()
    return link


def _resume_point(
    store: DuckStore, dataspec: str, start: str, force_setup: bool
) -> tuple[str, int]:
    """読み出し開始時刻と JVOpen の option。

    前回どこまで取ったかが ``_meta`` に残っていれば続きから、無ければ
    （あるいは取り直しを指示されていれば）セットアップから取る。
    """
    saved = store.meta(_META_PREFIX + dataspec)
    if saved and not force_setup:
        return saved, _OPTION_CONTINUE
    return start, _OPTION_SETUP


def _sync_dataspec(
    link: "JVLink",
    store: DuckStore,
    dataspec: str,
    fromtime: str,
    option: int,
    summary: SyncResult,
    *,
    log: Callable[[str], None],
    stop: Event | None,
    dry_run: bool,
) -> None:
    """1データ種別ぶんを取り込む。失敗しても呼び手は次の種別へ進む。"""
    from .jvlink import JVLinkError

    try:
        result = link.open(dataspec, fromtime, option)
    except JVLinkError as error:
        log(f"  取得できませんでした: {error}")
        summary.failed.append(dataspec)
        return
    log(
        f"  対象ファイル {result.read_count:,} 件 / 要ダウンロード "
        f"{result.download_count:,} 件"
    )
    if result.read_count == 0 or dry_run:
        if not dry_run:
            _remember_progress(store, dataspec, result)
        link.close()
        return
    try:
        _read_records(link, store, dataspec, result, summary, log=log, stop=stop)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001
        log(f"  読み込みに失敗しました: {error}")
        summary.failed.append(dataspec)
    finally:
        link.close()


def _read_records(
    link: "JVLink",
    store: DuckStore,
    dataspec: str,
    result: "OpenResult",
    summary: SyncResult,
    *,
    log: Callable[[str], None],
    stop: Event | None,
) -> None:
    """1データ種別ぶんを読み切って書き込む。"""
    link.wait_download(
        result,
        on_progress=lambda done, total: log(f"  ダウンロード {done:,}/{total:,}"),
    )
    written = 0
    started = time.time()
    for record in link.records(on_file=lambda name: log(f"  読込: {name}")):
        check_cancel(stop)
        store.write(record.data)
        written += 1
        if written % _PROGRESS_EVERY == 0:
            log(f"  {written:,} レコード ({time.time() - started:.0f}秒)")
    store.flush()
    # 読み切ってから記録する。途中で落ちたら次回もう一度同じ範囲を取る。
    _remember_progress(store, dataspec, result)
    summary.records += written
    log(f"  {written:,} レコード / {time.time() - started:.1f} 秒")


def _remember_progress(
    store: DuckStore, dataspec: str, result: "OpenResult"
) -> None:
    """次回の続きの起点を残す。タイムスタンプが取れないときは触らない。"""
    if result.last_file_timestamp:
        store.set_meta(_META_PREFIX + dataspec, result.last_file_timestamp)
