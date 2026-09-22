r"""古い年のデータだけを、期間を指定して DuckDB に追加で取り込む。

画面の「取得する」（``jvstore sync``）で期間を広げると、終わりの月を指定できないので、
すでに入っている新しい年まで読み直すことになる。このスクリプトは、足りない期間だけを読む。

    # 2016年1月〜2022年12月を、レース情報から順に取り込む
    uv run python tools/fetch_past_years.py --from 201601 --to 202212

    # 種別を絞る
    uv run python tools/fetch_past_years.py --from 202203 --to 202212 --dataspec RACE

    # 対象ファイル数だけ確かめる（DB は変えない）
    uv run python tools/fetch_past_years.py --from 201601 --to 202212 --dry-run

- 種別ごとに ``jvstore fetch --option 4``（セットアップ）を、読み出しの開始と終了を指定して呼ぶ。
- ``_meta``（差分取得の続きの位置）は動かさない。次の ``sync`` はこれまでどおり続きから取る。
- 取り込み中は画面と同じロック（``<DB>.ui.lock``）を取り、画面が同じ DB を開かないようにする。

**期間は月の単位で指定する。** セットアップのファイルは月ごとで、JV-Link はファイル名の
``年月99`` ＋提供時刻（例: ``2022129920230808…``）と、指定した時刻を文字として比べる。
終わりを ``20221231235959`` にすると ``20221299…`` の方が大きくなり、12月分が入らない。
そのため終わりは「最後の月の翌月1日0時」で渡す。

**1年ずつに区切って開く。** JV-Link は、一度に開くファイルが多いほど1レコードの読み出しが
遅くなる（JV-Linkインターフェース仕様書 4.9.0.1 p.17「既知の障害」）。実測で、140ファイルなら
0.08ミリ秒、1,260ファイルなら約2ミリ秒かかった。1年分のレース情報は約180ファイル。

終了時刻を指定できない種別（``DIFN`` ``HOSN`` ``HOYU`` ``COMM`` ``TOKU``）は対象にしない。
指定すると JV-Link が「該当データなし」を返す（同 p.18）。このうち ``DIFN`` ``HOSN`` ``HOYU``
``COMM`` は全件が提供されるので、``sync`` で一度取っていれば古い年の分も入っている。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jvstore.cli import main as jvstore  # noqa: E402
from jvstore.web.db_lock import DatabaseLock  # noqa: E402

#: 終了時刻を指定できる蓄積系の種別と、取り込む順番。
#: 予想に使うレース情報を先に、いちばんレコードの多い坂路調教（SLOP）を最後に置く。
DATASPECS = ("RACE", "SNPN", "MING", "BLDN", "WOOD", "YSCH", "SLOP")


def month(text: str) -> str:
    """``YYYYMM`` の形だけを受け付ける。"""
    if not re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", text):
        raise argparse.ArgumentTypeError(f"月は YYYYMM で指定してください: {text}")
    return text


def dataspecs(text: str) -> list[str]:
    """カンマ区切りの種別。終了時刻を指定できない種別は受け付けない。"""
    names = [name.strip().upper() for name in text.split(",") if name.strip()]
    unknown = [name for name in names if name not in DATASPECS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"期間を指定して取れない種別です: {', '.join(unknown)}"
            f"（指定できるのは {', '.join(DATASPECS)}）"
        )
    return names


def next_month(yyyymm: str) -> str:
    year, mon = int(yyyymm[:4]), int(yyyymm[4:])
    return f"{year + mon // 12:04d}{mon % 12 + 1:02d}"


def previous_month(yyyymm: str) -> str:
    year, mon = int(yyyymm[:4]), int(yyyymm[4:])
    return f"{year - (mon == 1):04d}{(mon - 2) % 12 + 1:02d}"


def yearly_ranges(first: str, last: str) -> list[tuple[str, str]]:
    """``first``〜``last`` の月を1年ずつに区切った、JVOpen の開始・終了時刻の組。

    終了は、その区切りの最後の月の翌月1日0時にする（12月分を落とさないため）。
    """
    stop = next_month(last)
    return [
        (f"{max(first, f'{year}01')}01000000", f"{min(stop, f'{year + 1}01')}01000000")
        for year in range(int(first[:4]), int(last[:4]) + 1)
    ]


def fetch_args(
    dataspec: str, fromtime: str, totime: str, db: Path, *, dry_run: bool
) -> list[str]:
    """1回ぶんの ``jvstore fetch`` の引数。"""
    args = [
        "fetch", "--dataspec", dataspec, "--from", fromtime, "--to", totime,
        "--option", "4", "--db", str(db),
    ]
    return args + ["--dry-run"] if dry_run else args


def fetch_all(args: argparse.Namespace) -> list[str]:
    """種別ごと・1年ごとに取り込む。失敗したものを返す。失敗しても残りは続ける。"""
    plan = [
        (dataspec, fromtime, totime)
        for dataspec in args.dataspec
        for fromtime, totime in yearly_ranges(args.first, args.last)
    ]
    failed = []
    for dataspec, fromtime, totime in plan:
        started = time.time()
        label = f"{dataspec} {fromtime[:6]}〜{previous_month(totime[:6])}"
        print(f"\n===== {label} 開始 {time.strftime('%H:%M:%S')}", flush=True)
        code = jvstore(fetch_args(dataspec, fromtime, totime, args.db, dry_run=args.dry_run))
        print(f"===== {label} 終了 code={code} {(time.time() - started) / 60:.1f} 分", flush=True)
        failed += [f"{dataspec} {fromtime[:4]}年"] if code != 0 else []
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--from", dest="first", required=True, type=month, help="取り込む最初の月 YYYYMM")
    ap.add_argument("--to", dest="last", required=True, type=month, help="取り込む最後の月 YYYYMM（この月も含む）")
    ap.add_argument(
        "--dataspec", type=dataspecs, default=list(DATASPECS),
        help=f"種別をカンマ区切りで限定（既定: {','.join(DATASPECS)}）",
    )
    ap.add_argument(
        "--db", type=Path, default=ROOT / "jvdata.duckdb",
        help="DuckDB の保存先（既定: このフォルダの jvdata.duckdb）",
    )
    ap.add_argument("--dry-run", action="store_true", help="対象ファイル数だけ確かめて終わる")
    args = ap.parse_args(argv)
    if args.first > args.last:
        ap.error(f"--from（{args.first}）が --to（{args.last}）より後になっています")

    lock = DatabaseLock(args.db)
    if not lock.acquire(timeout=5):
        print("DB を画面か別の取り込みが使用中です。終わってからやり直してください。", file=sys.stderr)
        return 1
    try:
        failed = fetch_all(args)
    finally:
        lock.release()
    if failed:
        print(f"\n失敗したもの: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
