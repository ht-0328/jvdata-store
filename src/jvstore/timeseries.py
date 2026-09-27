"""過去のレースの時系列オッズ（締め切り前の断面）を、期間を指定してまとめて DuckDB に入れる。

蓄積系（:mod:`jvstore.sync`）に入ってくるオッズは確定オッズだけで、締め切り前にいくらだったかは分からない。
速報オッズ（``0B30``、:mod:`jvstore.realtime`）は全賭式を取れるが、提供期間が1週間しかない。
時系列オッズは **提供期間が1年** あり、過去のレースでも発売開始から締め切りまでの断面が取れる
（単複枠 ``0B41``・馬連 ``0B42`` だけ。ワイド・馬単・3連複・3連単の時系列は仕様に無い）。
keiba-yosou が「買う時点のオッズでも同じ結果になるか」を過去のレースで確かめるのに使う。

レースの一覧は、先に蓄積系で入れた ``ra`` から拾う（``races_db``）。取り込み先（``db_path``）は
別のファイルにしてよい。数千レースを取るのに数時間かかり、その間ずっと元の DB を書き込みで開いたままに
すると、ほかのプロセスが元の DB を読めなくなる。別のファイルに貯めてから :mod:`jvstore.merge` で
元の DB へ移せば、元の DB を書き込みで開くのは最後の数分だけで済む。

途中で止めても続きから取り直せる。取り込み先に締め切り前の断面（データ区分 1〜3）がすでにあるレースは、
データ種別ごとに飛ばす。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable

import duckdb

from .layout import LayoutSet, load_layouts
from .realtime import RealtimeResult, _fetch_one, _initialised_link, parse_day
from .store import DuckStore
from .sync import check_cancel

__all__ = ["TIMESERIES_DATASPECS", "TimeseriesResult", "fetch_timeseries"]

#: 時系列オッズのデータ種別と、入る表。
TIMESERIES_DATASPECS: tuple[tuple[str, str, str], ...] = (
    ("0B41", "o1", "時系列オッズ（単複枠）"),
    ("0B42", "o2", "時系列オッズ（馬連）"),
)
#: 締め切り前の断面のデータ区分（1 中間・2 前日売最終・3 最終）。
_BEFORE_FINAL = ("1", "2", "3")
_JRA_VENUES = ("01", "10")
_RACE_KEY = (
    '"開催年" || "開催月日" || "競馬場コード" || "開催回[第N回]" || "開催日目[N日目]" || "レース番号"'
)


@dataclass
class TimeseriesResult:
    """1回の取得で何が入ったか。``skipped`` は取り込み済みで飛ばしたレースの数（データ種別ごと）。"""

    races: int = 0
    records: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    empty: dict[str, int] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


def fetch_timeseries(
    db_path: Path,
    first_day: str,
    last_day: str,
    *,
    races_db: Path | None = None,
    refetch: bool = False,
    log: Callable[[str], None] = print,
    stop: Event | None = None,
    layouts: LayoutSet | None = None,
    #: 初期化まで済ませた JV-Link を返す関数。省略すると自分で用意する。
    link_factory: Callable[[], Any] | None = None,
) -> TimeseriesResult:
    """``first_day``〜``last_day``（両端を含む）の中央の全レースの時系列オッズを ``db_path`` に入れる。

    ``races_db`` はレースの一覧を読む DB（省略すると ``db_path``）。``refetch`` を付けると、
    取り込み済みのレースも取り直す。1レースの失敗や「該当データなし」で全体は止めない。
    """
    from .jvlink import JVLink

    first, last = parse_day(first_day), parse_day(last_day)
    if first > last:
        raise ValueError(f"開始日（{first}）が終了日（{last}）より後になっています")
    races_by_day = _races_by_day(Path(races_db or db_path), first, last)
    result = TimeseriesResult(races=sum(len(races) for races in races_by_day.values()))
    log(f"{first}〜{last} の {len(races_by_day)} 開催日・{result.races:,} レースの時系列オッズを取得します")
    log(f"保存先: {Path(db_path).resolve()}")

    store = DuckStore(db_path, layouts or load_layouts())
    link = link_factory() if link_factory else _initialised_link(JVLink)
    try:
        done = {dataspec: set() if refetch else _fetched_races(store, table) for dataspec, table, _ in TIMESERIES_DATASPECS}
        for day, races in races_by_day.items():
            started = time.time()
            for race_key in races:
                check_cancel(stop)
                _fetch_race(link, store, race_key, done, result, log=log)
            written = ", ".join(f"{dataspec} {result.records.get(dataspec, 0):,}" for dataspec, _, _ in TIMESERIES_DATASPECS)
            log(f"{day[:4]}-{day[4:6]}-{day[6:]} {len(races)} レース / {time.time() - started:.0f} 秒（累計 {written} レコード）")
    finally:
        try:
            link.close()
        except Exception as error:  # noqa: BLE001
            log(f"JV-Link を閉じられませんでした: {error}")
        store.close()
    return result


def _fetch_race(
    link: Any,
    store: DuckStore,
    race_key: str,
    done: dict[str, set[str]],
    result: TimeseriesResult,
    *,
    log: Callable[[str], None],
) -> None:
    """1レースぶん、取り込み済みでないデータ種別だけを取る。"""
    for dataspec, _, _ in TIMESERIES_DATASPECS:
        if race_key in done[dataspec]:
            result.skipped[dataspec] = result.skipped.get(dataspec, 0) + 1
            continue
        summary = RealtimeResult()
        _fetch_one(link, store, dataspec, race_key, summary, log=log, label="", quiet=True)
        result.records[dataspec] = result.records.get(dataspec, 0) + summary.records.get(dataspec, 0)
        if summary.empty:
            result.empty[dataspec] = result.empty.get(dataspec, 0) + 1
        if summary.failed:
            result.failed.append(f"{dataspec} {race_key}")


def _races_by_day(races_db: Path, first: str, last: str) -> dict[str, list[str]]:
    """期間の中央のレースの要求キーを、開催日ごとに並べる。"""
    con = duckdb.connect(str(races_db), read_only=True)
    try:
        if not _has_table(con, "ra"):
            return {}
        rows = con.execute(
            f'SELECT DISTINCT "開催年" || "開催月日" AS day, {_RACE_KEY} AS race_key FROM ra '
            f'WHERE "開催年" || "開催月日" BETWEEN ? AND ? AND "競馬場コード" BETWEEN ? AND ? ORDER BY 2',
            [first, last, *_JRA_VENUES],
        ).fetchall()
    finally:
        con.close()
    races: dict[str, list[str]] = {}
    for day, race_key in rows:
        races.setdefault(day, []).append(race_key)
    return races


def _fetched_races(store: DuckStore, table: str) -> set[str]:
    """取り込み先に締め切り前の断面がすでにあるレースの要求キー。"""
    if not _has_table(store.con, table):
        return set()
    placeholders = ", ".join("?" for _ in _BEFORE_FINAL)
    rows = store.con.execute(
        f'SELECT DISTINCT {_RACE_KEY} FROM {table} WHERE "データ区分" IN ({placeholders})', list(_BEFORE_FINAL)
    ).fetchall()
    return {race_key for (race_key,) in rows}


def _has_table(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [table]).fetchone()[0])
