r"""古い年のデータだけを、期間を指定して DuckDB に追加で取り込む。

画面の「取得する」（``jvstore sync``）で期間を広げると、終わりの日を指定できないので、
すでに入っている新しい年まで読み直すことになる。JV-Link の読み出しは1レコード約4ミリ秒で、
坂路調教だけでも1年に約50万レコードあるため、読み直しに何時間もかかる。
このスクリプトは、足りない期間だけを読む。

    # 2016年1月〜2022年12月を、レース情報から順に取り込む
    uv run python tools/fetch_past_years.py --from 20160101 --to 20221231

    # 種別を絞る
    uv run python tools/fetch_past_years.py --from 20200201 --to 20221231 --dataspec RACE

    # 対象ファイル数だけ確かめる（DB は変えない）
    uv run python tools/fetch_past_years.py --from 20160101 --to 20221231 --dry-run

- 種別ごとに ``jvstore fetch --option 4``（セットアップ）を、読み出しの開始と終了を指定して呼ぶ。
  セットアップの開始・終了は、月単位のファイルの年月で効く。
- ``_meta``（差分取得の続きの位置）は動かさない。次の ``sync`` はこれまでどおり続きから取る。
- 取り込み中は画面と同じロック（``<DB>.ui.lock``）を取り、画面が同じ DB を開かないようにする。

終了日を指定できない種別（``DIFN`` ``HOSN`` ``HOYU`` ``COMM`` ``TOKU``）は対象にしない。
指定すると JV-Link が「該当データなし」を返す（JV-Linkインターフェース仕様書 4.9.0.1 p.18）。
このうち ``DIFN`` ``HOSN`` ``HOYU`` ``COMM`` は全件が提供されるので、``sync`` で一度取っていれば
古い年の分も入っている。
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

#: 終了日を指定できる蓄積系の種別と、取り込む順番。
#: 予想に使うレース情報を先に、いちばん時間のかかる坂路調教（SLOP）を最後に置く。
DATASPECS = ("RACE", "SNPN", "MING", "BLDN", "WOOD", "YSCH", "SLOP")


def day(text: str) -> str:
    """``YYYYMMDD`` の形だけを受け付ける。"""
    if not re.fullmatch(r"\d{8}", text):
        raise argparse.ArgumentTypeError(f"日付は YYYYMMDD で指定してください: {text}")
    return text


def dataspecs(text: str) -> list[str]:
    """カンマ区切りの種別。終了日を指定できない種別は受け付けない。"""
    names = [name.strip().upper() for name in text.split(",") if name.strip()]
    unknown = [name for name in names if name not in DATASPECS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"期間を指定して取れない種別です: {', '.join(unknown)}"
            f"（指定できるのは {', '.join(DATASPECS)}）"
        )
    return names


def fetch_args(dataspec: str, start: str, end: str, db: Path, *, dry_run: bool) -> list[str]:
    """1種別ぶんの ``jvstore fetch`` の引数。開始日は0時0分0秒、終了日は23時59分59秒にする。"""
    args = [
        "fetch", "--dataspec", dataspec,
        "--from", f"{start}000000", "--to", f"{end}235959",
        "--option", "4", "--db", str(db),
    ]
    return args + ["--dry-run"] if dry_run else args


def fetch_all(args: argparse.Namespace) -> list[str]:
    """種別ごとに取り込む。失敗した種別を返す。失敗しても残りの種別は続ける。"""
    failed = []
    for dataspec in args.dataspec:
        started = time.time()
        print(f"\n===== {dataspec} {args.start}〜{args.end} 開始 {time.strftime('%H:%M:%S')}", flush=True)
        code = jvstore(fetch_args(dataspec, args.start, args.end, args.db, dry_run=args.dry_run))
        print(f"===== {dataspec} 終了 code={code} {(time.time() - started) / 60:.1f} 分", flush=True)
        failed += [dataspec] if code != 0 else []
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--from", dest="start", required=True, type=day, help="取り込む最初の日 YYYYMMDD")
    ap.add_argument("--to", dest="end", required=True, type=day, help="取り込む最後の日 YYYYMMDD")
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
    if args.start > args.end:
        ap.error(f"--from（{args.start}）が --to（{args.end}）より後になっています")

    lock = DatabaseLock(args.db)
    if not lock.acquire(timeout=5):
        print("DB を画面か別の取り込みが使用中です。終わってからやり直してください。", file=sys.stderr)
        return 1
    try:
        failed = fetch_all(args)
    finally:
        lock.release()
    if failed:
        print(f"\n失敗した種別: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
