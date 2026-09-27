"""発走前の取り直しの段取りを、JV-Link と時計なしで固定する。

はじめに1日ぶんを取り、各レースの発走の少し前にそのレースのオッズを取り直し、最後に成績と払戻を取ることを、
偽の JV-Link と、待つと進む偽の時計で確かめる。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jvstore import load_layouts
from jvstore.realtime_follow import FollowClock, follow_day

from test_realtime import FakeLink
from test_store import RACE, make_record

LAYOUTS = load_layouts()
DAY = RACE["開催年"] + RACE["開催月日"]  # 20260906


class FakeTime:
    """待つと、その分だけ時刻が進む時計。"""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def clock(self) -> FollowClock:
        return FollowClock(now=lambda: self.current, sleep=self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=max(seconds, 1.0))


def _race(number: str, post: str) -> bytes:
    return make_record("RA", {**RACE, "レース番号": number, "発走時刻": post, "データ区分": "2", "データ作成年月日": DAY})


def _key(number: str) -> str:
    return DAY + RACE["競馬場コード"] + RACE["開催回[第N回]"] + RACE["開催日目[N日目]"] + number


def _follow(tmp_path, link: FakeLink, start: datetime):
    fake = FakeTime(start)
    log: list[str] = []
    follow_day(tmp_path / "db.duckdb", DAY, log=log.append, layouts=LAYOUTS, link_factory=lambda: link,
               clock=fake.clock())
    return fake, log


def test_各レースの発走の12分前にオッズを取り直し_最後に成績を取る(tmp_path):
    link = FakeLink({"0B15": [_race("01", "1000"), _race("02", "1030")]})
    fake, _ = _follow(tmp_path, link, datetime(2026, 9, 6, 8, 0))
    assert link.calls[-7:] == [("0B11", DAY), ("0B14", DAY), ("0B30", _key("01")),
                               ("0B11", DAY), ("0B14", DAY), ("0B30", _key("02")), ("0B15", DAY)]
    before_post = [call for call in link.calls if call == ("0B30", _key("02"))]
    assert len(before_post) == 2, "はじめの1日ぶんと、発走前の取り直しの2回"
    assert link.calls[-1] == ("0B15", DAY), "最後に成績と払戻を取る"
    assert fake.current >= datetime(2026, 9, 6, 10, 50), "最後のレースの発走から20分待つ"


def test_発走を過ぎたレースは取り直さない(tmp_path):
    link = FakeLink({"0B15": [_race("01", "1000"), _race("02", "1030")]})
    _follow(tmp_path, link, datetime(2026, 9, 6, 10, 25))
    assert link.calls.count(("0B30", _key("01"))) == 1, "はじめの1日ぶんだけ"
    assert link.calls.count(("0B30", _key("02"))) == 2


def test_取り直しは発走の12分前より早くはしない(tmp_path):
    times: list[datetime] = []
    link = FakeLink({"0B15": [_race("01", "1000")]})
    fake = FakeTime(datetime(2026, 9, 6, 8, 0))
    original = link.rt_open

    def spy(dataspec: str, key: str) -> bool:
        times.append(fake.current)
        return original(dataspec, key)

    link.rt_open = spy
    follow_day(tmp_path / "db.duckdb", DAY, log=lambda _: None, layouts=LAYOUTS, link_factory=lambda: link,
               clock=fake.clock())
    retaken = [at for at, call in zip(times, link.calls) if call == ("0B30", _key("01"))][1]
    assert datetime(2026, 9, 6, 9, 48) <= retaken < datetime(2026, 9, 6, 10, 0)
