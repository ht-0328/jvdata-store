"""開催日の間、各レースの発走の少し前に、そのレースの速報オッズと、その日の馬体重・開催情報を取り直し続ける。

:func:`jvstore.realtime.fetch_day` は、呼んだ時点の断面しか取らない。keiba-yosou の当日の予想とフォワードテスト
（締め切りの約10分前のオッズで「買ったつもり」を記録する）は、発走の少し前の断面が要る。人が発走のたびに
取り込み直さなくてよいように、開催日の間ずっと動かしておく。

段取り:

1. はじめに :func:`~jvstore.realtime.fetch_day` で、その日の速報を全部取る（出馬表・馬体重・オッズ …）。
2. レースを発走時刻の順に並べ、各レースの発走の ``minutes_before`` 分前になったら、そのレースの速報オッズ（``0B30``）と、
   その日の馬体重（``0B11``）・開催情報（``0B14``。取消・発走時刻の変更）を取る。発走時刻は毎回 DB から読み直す。
3. 最後のレースの発走から ``results_after`` 分たったら、速報レース情報（``0B15``。成績と払戻）を取って終わる。

DB は取るときだけ書き込みで開き、すぐ閉じる。同じ DB を keiba-yosou が読むので、開けなければ少し待ってやり直す。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Callable

import duckdb

from .layout import LayoutSet, load_layouts
from .realtime import RealtimeResult, _fetch_one, _initialised_link, fetch_day, parse_day
from .store import DuckStore
from .sync import check_cancel

__all__ = ["FollowClock", "follow_day"]

#: 発走前に取り直すもの。レース毎のキーで取るのは速報オッズだけ。
_BEFORE_POST: tuple[tuple[str, str], ...] = (("0B11", "day"), ("0B14", "day"), ("0B30", "race"))
#: 1回に待つ長さの上限（秒）。止める指示と発走時刻の変更に、この間隔で気づく。
_WAIT_STEP = 60.0
#: DB が開けないときに待つ長さ（秒）と、あきらめるまでの回数。
_RETRY_WAIT, _RETRY_TIMES = 10.0, 30
_JRA_VENUES = ("01", "10")


@dataclass
class FollowClock:
    """今の時刻と待ち方。テストで差し替える。"""

    now: Callable[[], datetime] = field(default=datetime.now)
    sleep: Callable[[float], None] = field(default=time.sleep)


def follow_day(
    db_path: Path,
    day: str,
    *,
    minutes_before: int = 12,
    results_after: int = 20,
    log: Callable[[str], None] = print,
    stop: Event | None = None,
    layouts: LayoutSet | None = None,
    link_factory: Callable[[], Any] | None = None,
    clock: FollowClock | None = None,
) -> RealtimeResult:
    """開催日 ``day`` の速報を、各レースの発走の ``minutes_before`` 分前に取り直し続ける。最後に成績と払戻を取る。"""
    key = parse_day(day)
    layouts = layouts or load_layouts()
    clock = clock or FollowClock()
    summary = _retry(lambda: fetch_day(db_path, key, log=log, stop=stop, layouts=layouts, link_factory=link_factory),
                     clock, log)
    fetched: set[str] = set()
    while True:
        check_cancel(stop)
        pending = [(race_key, post) for race_key, post in _schedule(db_path, key, clock, log) if race_key not in fetched]
        if not pending:
            break
        race_key, post = pending[0]
        due = post - timedelta(minutes=minutes_before)
        if clock.now() < due:
            clock.sleep(min(_WAIT_STEP, (due - clock.now()).total_seconds()))
            continue
        fetched.add(race_key)
        if clock.now() >= post:
            log(f"{race_key}: 発走を過ぎているので取りません")
            continue
        _retry(lambda: _fetch_before_post(db_path, key, race_key, summary, layouts, link_factory, log), clock, log)
    _fetch_results(db_path, key, results_after, summary, layouts, link_factory, clock, log, stop)
    return summary


def _fetch_before_post(
    db_path: Path, day: str, race_key: str, summary: RealtimeResult, layouts: LayoutSet,
    link_factory: Callable[[], Any] | None, log: Callable[[str], None],
) -> None:
    """1レースの発走前に、そのレースのオッズと、その日の馬体重・開催情報を取る。"""
    log(f"{race_key[8:10]}場 {race_key[-2:]}R の発走前の速報を取ります")
    _fetch(db_path, [(dataspec, race_key if unit == "race" else day) for dataspec, unit in _BEFORE_POST],
           summary, layouts, link_factory, log)


def _fetch_results(
    db_path: Path, day: str, results_after: int, summary: RealtimeResult, layouts: LayoutSet,
    link_factory: Callable[[], Any] | None, clock: FollowClock, log: Callable[[str], None], stop: Event | None,
) -> None:
    """最後のレースの発走から ``results_after`` 分たったら、成績と払戻（0B15）を取る。"""
    posts = [post for _, post in _schedule(db_path, day, clock, log)]
    due = (max(posts) if posts else clock.now()) + timedelta(minutes=results_after)
    while clock.now() < due:
        check_cancel(stop)
        clock.sleep(min(_WAIT_STEP, (due - clock.now()).total_seconds()))
    log("成績と払戻を取ります")
    _retry(lambda: _fetch(db_path, [("0B15", day)], summary, layouts, link_factory, log), clock, log)


def _fetch(
    db_path: Path, requests: list[tuple[str, str]], summary: RealtimeResult, layouts: LayoutSet,
    link_factory: Callable[[], Any] | None, log: Callable[[str], None],
) -> None:
    from .jvlink import JVLink

    store = DuckStore(db_path, layouts)
    link = link_factory() if link_factory else _initialised_link(JVLink)
    try:
        for dataspec, request_key in requests:
            _fetch_one(link, store, dataspec, request_key, summary, log=log, label="", quiet=True)
    finally:
        link.close()
        store.close()


def _schedule(db_path: Path, day: str, clock: FollowClock, log: Callable[[str], None]) -> list[tuple[str, datetime]]:
    """その日の中央のレースの（要求キー, 発走の日時）を、発走の順に。"""
    rows = _retry(lambda: _read_schedule(db_path, day), clock, log)
    return sorted(((race_key, datetime.strptime(day + post, "%Y%m%d%H%M")) for race_key, post in rows),
                  key=lambda item: (item[1], item[0]))


def _read_schedule(db_path: Path, day: str) -> list[tuple[str, str]]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        exists = con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'ra'").fetchone()[0]
        if not exists:
            return []
        return con.execute(
            'SELECT "開催年" || "開催月日" || "競馬場コード" || "開催回[第N回]" || "開催日目[N日目]" || "レース番号", '
            'max("発走時刻") FROM ra WHERE "開催年" = ? AND "開催月日" = ? AND "競馬場コード" BETWEEN ? AND ? '
            "AND \"データ区分\" NOT IN ('0', '9') AND \"発走時刻\" NOT IN ('', '0000') GROUP BY 1",
            [day[:4], day[4:], *_JRA_VENUES],
        ).fetchall()
    finally:
        con.close()


def _retry(action: Callable[[], Any], clock: FollowClock, log: Callable[[str], None]) -> Any:
    """DB がほかのプロセスに使われていて開けないときは、少し待ってやり直す。"""
    for attempt in range(_RETRY_TIMES):
        try:
            return action()
        except duckdb.IOException as error:
            if attempt == _RETRY_TIMES - 1:
                raise
            log(f"DB を開けないので {_RETRY_WAIT:.0f} 秒待ってやり直します（{error.__class__.__name__}）")
            clock.sleep(_RETRY_WAIT)
    return None
