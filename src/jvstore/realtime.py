"""開催日の速報系データを、まとめて DuckDB に入れる。

蓄積系（:mod:`jvstore.sync`）に入ってくるのは、確定したあとのデータだけ。発走前に要る
オッズ・馬体重・天候と馬場状態・マイニング予想・出馬表の変更（取消・騎手変更）は、速報系
（``JVRTOpen``）でしか取れない。ここでは **開催日を1つ指定して、その日の速報を全部取る**。
レースや馬で絞り込む機能は持たない。絞り込みは読む側（keiba-yosou）の責務。

提供の単位はデータ種別ごとに決まっている（JV-Data仕様書「データ提供タイミング･提供単位」）。

- 開催日単位（キーは ``YYYYMMDD``）: 速報レース情報・速報開催情報・速報馬体重・マイニング予想
- レース毎（キーは ``YYYYMMDDJJKKHHRR``）: 速報オッズ。その日のレースは、先に取った速報レース情報
  （無ければ蓄積系の ``ra``）から拾う

速報の提供期間は1週間。まだ発表されていないデータ（前日の馬体重など）は「該当データなし」で、
エラーにはしない。同じ日を何度取り直してもよい（同じ鍵の行は新しい版に置き換わり、オッズは
発表時刻ごとに行が増える）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, Callable

from .layout import LayoutSet, load_layouts
from .store import DuckStore
from .sync import check_cancel

if TYPE_CHECKING:  # JV-Link は Windows + COM が必要なので実行時には読み込まない
    from .jvlink import JVLink

__all__ = ["DAY_DATASPECS", "RACE_DATASPECS", "RealtimeResult", "fetch_day", "parse_day"]

#: 開催日単位で取る速報。先頭の速報レース情報が、その日のレースの一覧（ra）も運んでくる。
DAY_DATASPECS: tuple[tuple[str, str], ...] = (
    ("0B15", "速報レース情報（出馬表・取消・騎手変更の反映、当日は成績）"),
    ("0B14", "速報開催情報（天候・馬場状態、取消、騎手変更、発走時刻変更、コース変更）"),
    ("0B11", "速報馬体重"),
    ("0B13", "速報タイム型データマイニング予想"),
    ("0B17", "速報対戦型データマイニング予想"),
)
#: レース毎に取る速報。
RACE_DATASPECS: tuple[tuple[str, str], ...] = (
    ("0B31", "速報オッズ（単勝・複勝・枠連）"),
)
#: 中央競馬の競馬場コードの範囲。速報オッズは中央のレースにしか無い。
_JRA_VENUES = ("01", "10")
_DAY_DIGITS = 8


@dataclass
class RealtimeResult:
    """1回の取得で何が入ったか。``records`` は データ種別 → レコード数。"""

    records: dict[str, int] = field(default_factory=dict)
    empty: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    races: int = 0


def parse_day(text: str) -> str:
    """``2026-09-20`` か ``20260920`` を ``YYYYMMDD`` にする。"""
    digits = str(text).strip().replace("-", "").replace("/", "")
    try:
        if len(digits) != _DAY_DIGITS:
            raise ValueError
        date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        raise ValueError(f"開催日は 2026-09-20 か 20260920 の形で指定してください: {text}") from None
    return digits


def fetch_day(
    db_path: Path,
    day: str,
    *,
    log: Callable[[str], None] = print,
    stop: Event | None = None,
    layouts: LayoutSet | None = None,
    #: 初期化まで済ませた JV-Link を返す関数。省略すると自分で用意する。
    link_factory: Callable[[], Any] | None = None,
) -> RealtimeResult:
    """開催日 ``day`` の速報を全部取り込む。

    途中で失敗したデータ種別やレースがあっても、残りは続ける。発走前に1つ取れないだけで
    全部をやり直すことになるのを避ける。
    """
    from .jvlink import JVLink

    key = parse_day(day)
    layouts = layouts or load_layouts()
    summary = RealtimeResult()
    log(f"{key[:4]}-{key[4:6]}-{key[6:]} の速報を取得します")
    log(f"保存先: {db_path.resolve()}")

    store = DuckStore(db_path, layouts)
    link = link_factory() if link_factory else _initialised_link(JVLink)
    try:
        for dataspec, title in DAY_DATASPECS:
            check_cancel(stop)
            _fetch_one(link, store, dataspec, key, summary, log=log, label=f"{dataspec} {title}")
        races = _race_keys(store, key)
        summary.races = len(races)
        for dataspec, title in RACE_DATASPECS:
            log("")
            log(f"{dataspec} {title} — {len(races)} レース")
            if not races:
                log("  その日のレースが DB にありません（出馬表の発表前か、開催の無い日）")
            for race_key in races:
                check_cancel(stop)
                _fetch_one(link, store, dataspec, race_key, summary, log=log, label="", quiet=True)
            log(f"  {summary.records.get(dataspec, 0):,} レコード")
    finally:
        try:
            link.close()
        except Exception as error:  # noqa: BLE001
            log(f"JV-Link を閉じられませんでした: {error}")
        store.close()
    return summary


def _initialised_link(factory: Callable[[], Any]) -> Any:
    link = factory()
    link.init()
    return link


def _fetch_one(
    link: "JVLink",
    store: DuckStore,
    dataspec: str,
    key: str,
    summary: RealtimeResult,
    *,
    log: Callable[[str], None],
    label: str,
    quiet: bool = False,
) -> None:
    """1つのデータ種別・1つのキーぶんを取り込む。失敗しても呼び手は次へ進む。"""
    from .jvlink import JVLinkError

    if label:
        log("")
        log(label)
    started = time.time()
    written = 0
    try:
        if not link.rt_open(dataspec, key):
            if not quiet:
                log("  該当データなし（まだ発表されていないか、提供期間の1週間を過ぎています）")
            if dataspec not in summary.empty:
                summary.empty.append(dataspec)
            return
        for record in link.records():
            store.write(record.data)
            written += 1
        store.flush()
    except JVLinkError as error:
        log(f"  取得できませんでした（{dataspec} {key}）: {error}")
        if dataspec not in summary.failed:
            summary.failed.append(dataspec)
        return
    finally:
        link.close()
    summary.records[dataspec] = summary.records.get(dataspec, 0) + written
    if not quiet:
        log(f"  {written:,} レコード / {time.time() - started:.1f} 秒")


def _race_keys(store: DuckStore, day: str) -> list[str]:
    """その日の中央のレースの要求キー（``YYYYMMDDJJKKHHRR``）。``ra`` がまだ無ければ空。"""
    exists = store.con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'ra'"
    ).fetchone()[0]
    if not exists:
        return []
    rows = store.con.execute(
        'SELECT DISTINCT "開催年" || "開催月日" || "競馬場コード" || "開催回[第N回]" || "開催日目[N日目]" || "レース番号" '
        'FROM ra WHERE "開催年" = ? AND "開催月日" = ? AND "競馬場コード" BETWEEN ? AND ? ORDER BY 1',
        [day[:4], day[4:], *_JRA_VENUES],
    ).fetchall()
    return [key for (key,) in rows]
