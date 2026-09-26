"""過去N年ぶんの蓄積系データを、まとめて DuckDB に入れる。

このモジュールが取得の本体で、コマンドラインも画面もここを呼ぶ。
**選ぶのは「何年ぶん」と「どのデータ種別か」だけ**にしてある。
レースや馬で絞り込む機能は持たない。絞り込みは読む側（keiba-yosou）の責務。

データ種別ごとに、初回はセットアップ（option=4）、2回目以降は前回の続き（option=1）。
どこまで取ったかは DuckDB の ``_meta`` に持つので、状態ファイルは要らない。

**セットアップは1年ずつに区切って開く。** JV-Link は、一度に開くファイルが多いほど
1レコードの読み出しが遅くなる（JV-Linkインターフェース仕様書 4.9.0.1 p.17「既知の障害」）。
15年分のレース情報を一度に開くと約2,800ファイルになり、1か月分の読み出しに12分かかった。
1年分なら約170ファイルで済む。
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
    from .jvlink import BrokenFileError, JVLink, OpenResult

__all__ = [
    "RANGED_DATASPECS", "SYNC_DATASPECS", "Cancelled", "SyncResult",
    "check_cancel", "open_times", "sync",
]

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

#: 読み出しの終わりの時刻を指定できる種別。セットアップを1年ずつに区切って開く。
#: ほかの種別（DIFN HOSN HOYU COMM TOKU）は終わりを指定すると「該当データなし」になる
#: （JV-Linkインターフェース仕様書 4.9.0.1 p.18）ので、これまでどおり一度に開く。
RANGED_DATASPECS = frozenset({"RACE", "SNPN", "MING", "BLDN", "WOOD", "YSCH", "SLOP"})

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

#: 壊れたファイルを消して開き直す回数の上限（1回の読み出し範囲ごと）。
#: 消しても同じエラーが続くときに、同じ範囲を延々と読み直さないため。
_MAX_REOPEN = 3

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


def open_times(
    dataspec: str, fromtime: str, option: int, today: date | None = None
) -> list[str]:
    """1データ種別を取り込むときに ``JVOpen`` へ順に渡す読み出し時刻。

    セットアップで、終わりを指定できる種別なら、開始の年から1年ずつに区切る。
    区切りの終わりは翌年1月1日0時にする。ファイル名の ``年月99``（例: ``20111299…``）と
    文字として比べられるので、12月31日にすると12月分が外れる。
    **今年の分だけは終わりを指定しない。** 最新のファイルまで読み、その時刻を
    次回の続きの起点にするため。
    """
    if option != _OPTION_SETUP or dataspec not in RANGED_DATASPECS:
        return [fromtime]
    this_year = (today or date.today()).year
    past = [
        f"{year:04d}0101000000-{year + 1:04d}0101000000"
        for year in range(int(fromtime[:4]), this_year)
    ]
    return past + [f"{this_year:04d}0101000000"]


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
    """1データ種別ぶんを取り込む。失敗しても呼び手は次の種別へ進む。

    1年ずつに区切ったときは、全部の年を読み終えてから続きの起点を残す。
    途中の年で失敗したら残さないので、次回また最初の年から取り直す。
    """
    times = open_times(dataspec, fromtime, option)
    latest = ""
    for opentime in times:
        check_cancel(stop)
        if len(times) > 1:
            log(f"  -- {opentime[:4]}年")
        timestamp = _sync_range(
            link, store, opentime, dataspec, option, summary, log=log, stop=stop, dry_run=dry_run
        )
        if timestamp is None:
            summary.failed.append(dataspec)
            return
        latest = max(latest, timestamp)
    if not dry_run:
        _remember_progress(store, dataspec, latest)


def _sync_range(
    link: "JVLink",
    store: DuckStore,
    opentime: str,
    dataspec: str,
    option: int,
    summary: SyncResult,
    *,
    log: Callable[[str], None],
    stop: Event | None,
    dry_run: bool,
) -> str | None:
    """1回の ``JVOpen`` で開けるぶんを読み切る。最新ファイルの時刻を返し、失敗なら None。

    保存パスのファイルが壊れていたら、そのファイルを消して ``JVOpen`` からやり直す
    （インターフェース仕様書 p.33「JVFiledelete」）。消したファイルは開き直すときに
    ダウンロードし直される。読み終えたレコードをもう一度書いても行は増えない。
    """
    from .jvlink import BrokenFileError

    for _ in range(_MAX_REOPEN):
        try:
            return _open_and_read(
                link, store, opentime, dataspec, option, summary,
                log=log, stop=stop, dry_run=dry_run,
            )
        except BrokenFileError as error:
            log(f"  {error}")
            if not _delete_broken(link, error, log=log):
                return None
            log("  壊れたファイルを消しました。開き直してダウンロードし直します")
    log(f"  {_MAX_REOPEN}回開き直しても読めませんでした")
    return None


def _delete_broken(
    link: "JVLink", error: "BrokenFileError", *, log: Callable[[str], None]
) -> bool:
    """壊れたファイルを消す。消すファイルが分からない・消せないなら False。"""
    from .jvlink import JVLinkError

    names = [error.filename] if error.filename else link.empty_files()
    if not names:
        log("  壊れたファイルの名前が分からないので、消せませんでした")
        return False
    try:
        for name in names:
            log(f"  削除: {name}")
            link.file_delete(name)
    except JVLinkError as delete_error:
        log(f"  削除できませんでした: {delete_error}")
        return False
    return True


def _open_and_read(
    link: "JVLink",
    store: DuckStore,
    opentime: str,
    dataspec: str,
    option: int,
    summary: SyncResult,
    *,
    log: Callable[[str], None],
    stop: Event | None,
    dry_run: bool,
) -> str | None:
    """``JVOpen`` して読み切る。壊れたファイルに当たったら BrokenFileError を投げる。"""
    from .jvlink import BrokenFileError, JVLinkError

    try:
        result = link.open(dataspec, opentime, option)
    except JVLinkError as error:
        log(f"  取得できませんでした: {error}")
        return None
    log(
        f"  対象ファイル {result.read_count:,} 件 / 要ダウンロード "
        f"{result.download_count:,} 件"
    )
    try:
        if result.read_count > 0 and not dry_run:
            _read_records(link, store, result, summary, log=log, stop=stop)
    except (Cancelled, BrokenFileError):
        raise
    except Exception as error:  # noqa: BLE001
        log(f"  読み込みに失敗しました: {error}")
        return None
    finally:
        link.close()
    return result.last_file_timestamp


def _read_records(
    link: "JVLink",
    store: DuckStore,
    result: "OpenResult",
    summary: SyncResult,
    *,
    log: Callable[[str], None],
    stop: Event | None,
) -> None:
    """開いたぶんを読み切って書き込む。"""
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
    summary.records += written
    log(f"  {written:,} レコード / {time.time() - started:.1f} 秒")


def _remember_progress(store: DuckStore, dataspec: str, timestamp: str) -> None:
    """次回の続きの起点を残す。タイムスタンプが取れないときは触らない。

    読み切ってから呼ぶ。途中で落ちたら次回もう一度同じ範囲を取る。
    """
    if timestamp:
        store.set_meta(_META_PREFIX + dataspec, timestamp)
