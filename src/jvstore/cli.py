"""jvstore コマンドラインインターフェース。

    jvstore sync --years 10            過去N年ぶんの蓄積系データをまとめて取得する
    jvstore fetch --dataspec RACE ...  蓄積系データ(JVOpen)を期間を指定して取得する
    jvstore rt --dataspec 0B15 ...     速報系データ(JVRTOpen)を取得する
    jvstore parse raw/ ...             保存済みの生データを読み込む（JV-Link 不要）
    jvstore spec build --xlsx ...      JV-Data仕様書 xlsx からレイアウト定義を生成
    jvstore spec list                  取り込める表（レコード種別）とデータ種別IDの一覧
    jvstore spec columns RA            ある表のカラム定義を表示
    jvstore setup                      JV-Link の設定画面（利用キー登録）を開く

出力先は既定で DuckDB。CSV が要るときだけ `--out` を付ける。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .layout import LayoutSet, load_layouts
from .record import FlatLayout
from .store import DuckStore
from .writer import CsvSink

DEFAULT_LAYOUTS = Path(__file__).with_name("resources") / "layouts.json"


# --------------------------------------------------------------------- 共通
def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _load(args: argparse.Namespace) -> LayoutSet:
    return load_layouts(Path(args.layouts) if getattr(args, "layouts", None) else None)


def _state_load(path: Path | None) -> dict[str, dict[str, str]]:
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _state_save(path: Path | None, dataspec: str, timestamp: str) -> None:
    if not path or not timestamp:
        return
    state = _state_load(path)
    state[dataspec] = {
        "last_file_timestamp": timestamp,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _report(stats: Counter[str], sink: CsvSink | DuckStore) -> None:
    _log("")
    _log("--- 出力結果（表＝レコード種別ごと） ---")
    skipped: list[str] = []
    for rid, n in sorted(stats.items()):
        if rid.startswith("("):
            continue
        base = rid.split("(")[0]
        layout = sink.layouts.get(base)
        name = layout.title if layout else "?"
        if rid.endswith("(除外)"):
            skipped.append(f"{base} {name} {n:,}件")
            continue
        _log(f"  {base} {name:<16} {n:>9,} 件")
    if skipped:
        _log(f"  --only 指定により出力しなかった表: {', '.join(skipped)}")
    unknown = stats.get("(未知のレコード種別)", 0)
    if unknown:
        ids = sorted(k[4:] for k in stats if k.startswith("(未知)"))
        _log(f"  未知のレコード種別: {unknown:,} 件 {ids}")
    for p in sink.written_files():
        _log(f"  -> {p}")


# --------------------------------------------------------------------- spec
def cmd_spec_build(args: argparse.Namespace) -> int:
    from .spec_parser import parse_spec_workbook, validate

    xlsx = Path(args.xlsx)
    out = Path(args.out) if args.out else DEFAULT_LAYOUTS
    _log(f"仕様書を読み込み中: {xlsx}")
    ls = parse_spec_workbook(xlsx)
    ng = 0
    for layout in ls:
        problems = validate(layout)
        for p in problems:
            _log(f"  [警告] {p}")
        ng += bool(problems)
    ls.dump(out)
    _log(f"表 {len(ls.layouts)} 件、データ種別 {len(ls.dataspecs)} 件を書き出しました: {out}")
    if ng:
        _log(f"[警告] {ng} 表で位置の整合性エラーがあります")
    return 1 if ng and args.strict else 0


def cmd_spec_list(args: argparse.Namespace) -> int:
    ls = _load(args)
    print(f"# JV-Data仕様書 {ls.version} ({ls.source})")
    print()
    print("## 表（レコード種別）")
    print(f"{'表番号':>6} {'ID':<4} {'表題':<22} {'レコード長':>9} {'カラム数':>8}")
    for layout in sorted(ls, key=lambda l: int(l.index)):
        flat = FlatLayout(layout)
        print(
            f"{layout.index:>6} {layout.record_id:<4} {layout.title:<22} "
            f"{layout.length:>9,} {len(flat.columns):>8,}"
        )
    if ls.dataspecs:
        print()
        print("## データ種別ID（--dataspec に指定する値）")
        for spec in ls.dataspecs.values():
            cat = "/".join(spec.categories) or "-"
            print(f"  {spec.id:<6} {cat:<20} {spec.name}")
            print(f"         収録レコード: {' '.join(spec.record_ids)}")
    return 0


def cmd_spec_columns(args: argparse.Namespace) -> int:
    ls = _load(args)
    layout = ls.get(args.record_id.upper())
    if layout is None:
        _log(f"レコード種別 {args.record_id} は定義にありません")
        return 1
    flat = FlatLayout(layout, keep_separator=args.keep_separator)
    print(f"# {layout.record_id} {layout.title} （表 {layout.index}／{layout.length} バイト）")
    print(f"{'項番':<6} {'位置':>7} {'長':>5} キー カラム名")
    for c in flat.columns:
        print(
            f"{c.item_no:<6} {c.offset + 1:>7} {c.size:>5} "
            f"{'○' if c.is_key else ' ':<3} {c.name}"
        )
    print(f"\nカラム数: {len(flat.columns):,}")
    return 0


# -------------------------------------------------------------------- 取得
def _consume(
    records: Iterator[tuple[bytes, str]],
    args: argparse.Namespace,
    layouts: LayoutSet,
    raw_dir: Path | None,
) -> Counter[str]:
    """レコード列を DuckDB（--out 指定時は CSV）に書き出す。"""
    only = args.only.split(",") if args.only else None
    if args.out:
        outdir = Path(args.out)
        sink = CsvSink(
            outdir,
            layouts,
            encoding=args.encoding,
            strip=not args.keep_padding,
            keep_separator=args.keep_separator,
            only=only,
            append=args.append,
            unknown_path=outdir / "_unknown_records.txt" if args.save_unknown else None,
        )
    else:
        sink = DuckStore(
            Path(args.db), layouts, only=only, strip=not args.keep_padding
        )
    raw_files: dict[str, object] = {}
    started = time.time()
    n = 0
    try:
        for data, fname in records:
            sink.write(data)
            n += 1
            if raw_dir is not None:
                key = fname or "unknown.jvd"
                f = raw_files.get(key)
                if f is None:
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    f = raw_files[key] = (raw_dir / key).open("wb")
                f.write(data)  # type: ignore[union-attr]
            if args.limit and n >= args.limit:
                _log(f"--limit {args.limit} に達したので読み込みを打ち切ります")
                break
            if n % 50000 == 0:
                _log(f"  {n:,} レコード処理 ({time.time() - started:.0f}秒)")
    finally:
        sink.close()
        for f in raw_files.values():
            f.close()  # type: ignore[union-attr]
    _log(f"合計 {n:,} レコード / {time.time() - started:.1f} 秒")
    _report(sink.stats, sink)
    return sink.stats


def _jvlink(args: argparse.Namespace):
    from .jvlink import JVLink

    link = JVLink(args.sid)
    link.init()
    if args.save_files is not None:
        link.set_save_flag(1 if args.save_files else 0)
    return link


def cmd_fetch(args: argparse.Namespace) -> int:
    from .jvlink import JVLinkError

    layouts = _load(args)
    state_path = Path(args.state) if args.state else None
    fromtime = args.from_
    if fromtime in (None, "", "auto"):
        saved = _state_load(state_path).get(args.dataspec, {})
        fromtime = saved.get("last_file_timestamp", "")
        if not fromtime:
            _log("前回取得時刻が不明です。--from に YYYYMMDDhhmmss を指定してください。")
            return 2
        _log(f"前回の続き（{fromtime}）から取得します")
    if args.to:
        fromtime = f"{fromtime}-{args.to}"

    link = _jvlink(args)
    try:
        _log(f"JVOpen dataspec={args.dataspec} fromtime={fromtime} option={args.option}")
        result = link.open(args.dataspec, fromtime, args.option)
        _log(
            f"対象ファイル {result.read_count} 件 / 要ダウンロード {result.download_count} 件 "
            f"/ 最新タイムスタンプ {result.last_file_timestamp or '-'}"
        )
        if result.read_count == 0:
            _log("該当データがありません。")
            return 0
        if args.dry_run:
            return 0
        link.wait_download(
            result,
            on_progress=lambda d, t: _log(f"  ダウンロード {d}/{t}"),
        )

        def gen() -> Iterator[tuple[bytes, str]]:
            for rec in link.records(on_file=lambda f: _log(f"  読込: {f}")):
                yield rec.data, rec.filename

        _consume(gen(), args, layouts, Path(args.save_raw) if args.save_raw else None)
        if not args.limit:
            _state_save(state_path, args.dataspec, result.last_file_timestamp)
        return 0
    except JVLinkError as e:
        _log(str(e))
        return 1
    finally:
        link.close()


def cmd_rt(args: argparse.Namespace) -> int:
    from .jvlink import JVLinkError

    layouts = _load(args)
    link = _jvlink(args)
    try:
        _log(f"JVRTOpen dataspec={args.dataspec} key={args.key}")
        link.rt_open(args.dataspec, args.key)

        def gen() -> Iterator[tuple[bytes, str]]:
            for rec in link.records():
                yield rec.data, rec.filename

        _consume(gen(), args, layouts, Path(args.save_raw) if args.save_raw else None)
        return 0
    except JVLinkError as e:
        _log(str(e))
        return 1
    finally:
        link.close()


def cmd_parse(args: argparse.Namespace) -> int:
    layouts = _load(args)

    def gen() -> Iterator[tuple[bytes, str]]:
        for pattern in args.raw:
            for path in sorted(_expand(pattern)):
                _log(f"  読込: {path}")
                with path.open("rb") as f:
                    for line in f:
                        line = line.rstrip(b"\r\n")
                        if line:
                            yield line, path.name

    _consume(gen(), args, layouts, None)
    return 0


def _expand(pattern: str) -> Iterable[Path]:
    p = Path(pattern)
    if p.exists():
        return [p] if p.is_file() else sorted(p.glob("*"))
    return sorted(Path().glob(pattern))

# --------------------------------------------------------------------- sync
def cmd_sync(args: argparse.Namespace) -> int:
    """過去N年ぶんの蓄積系データを、種別を順に回してまとめて DuckDB へ入れる。

    取得の本体は :mod:`jvstore.sync` にある。keiba-yosou の Web 画面も
    このコマンドを呼ぶので、コマンドラインと画面で挙動がずれない。
    """
    from .sync import SYNC_DATASPECS, sync

    specs = (
        [s.strip().upper() for s in args.dataspec.split(",")]
        if args.dataspec
        else [d for d, _ in SYNC_DATASPECS]
    )
    result = sync(
        Path(args.db),
        years=args.years,
        dataspecs=specs,
        log=_log,
        layouts=_load(args),
        link_factory=lambda: _jvlink(args),
        only=args.only.split(",") if args.only else None,
        dry_run=args.dry_run,
        force_setup=args.force_setup,
    )

    _log("")
    _log("--- テーブルごとの行数 ---")
    for name, n in result.counts.items():
        _log(f"  {name:<28} {n:>12,}")
    if result.failed:
        _log(f"取得できなかったデータ種別: {', '.join(result.failed)}")
        return 1
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    link = _jvlink(args)
    _log("JV-Link の設定ダイアログを開きます（利用キーの登録・利用規約への同意）")
    link.set_ui_properties()
    return 0


# --------------------------------------------------------------------- 引数
def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default="jvdata.duckdb", help="DuckDB の保存先（既定: jvdata.duckdb）")
    p.add_argument("--out", help="CSV 出力先ディレクトリ（指定したときだけ CSV にする）")
    p.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="CSV の文字コード（既定: utf-8-sig／Excel 用は cp932 も可）",
    )
    p.add_argument("--only", help="出力するレコード種別IDをカンマ区切りで限定（例: RA,SE）")
    p.add_argument("--append", action="store_true", help="既存 CSV に追記する")
    p.add_argument(
        "--keep-padding", action="store_true", help="項目値の右側の空白を削らない"
    )
    p.add_argument(
        "--keep-separator", action="store_true", help="レコード区切(CR/LF)列も出力する"
    )
    p.add_argument("--save-unknown", action="store_true", help="未知のレコードを保存する")
    p.add_argument("--limit", type=int, help="読み込むレコード数の上限（動作確認用）")
    p.add_argument("--layouts", help="レイアウト定義 JSON のパス（既定: 同梱の layouts.json）")


def _add_jvlink_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sid", default="UNKNOWN", help="JVInit に渡すソフトウェアID")
    p.add_argument("--save-raw", help="受信した生データをこのディレクトリに保存する")
    p.add_argument(
        "--save-files",
        type=int,
        choices=(0, 1),
        help="JVSetSaveFlag: ダウンロードした JV-Data ファイルを残すか",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jvstore",
        description="JRA-VAN Data Lab.(JV-Link) のデータを JV-Data仕様書の表単位で DuckDB に貯める",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("spec", help="仕様書（レイアウト定義）の生成・参照")
    ssub = sp.add_subparsers(dest="spec_command", required=True)

    b = ssub.add_parser("build", help="JV-Data仕様書 xlsx からレイアウト定義を生成")
    b.add_argument("--xlsx", required=True, help="JV-Data仕様書_x.x.x.x.xlsx のパス")
    b.add_argument("--out", help=f"出力先 JSON（既定: {DEFAULT_LAYOUTS}）")
    b.add_argument("--strict", action="store_true", help="整合性エラーがあれば異常終了する")
    b.set_defaults(func=cmd_spec_build)

    l = ssub.add_parser("list", help="表とデータ種別IDの一覧")
    l.add_argument("--layouts")
    l.set_defaults(func=cmd_spec_list)

    c = ssub.add_parser("columns", help="表のカラム定義を表示")
    c.add_argument("record_id", help="レコード種別ID（RA, SE, UM …）")
    c.add_argument("--keep-separator", action="store_true")
    c.add_argument("--layouts")
    c.set_defaults(func=cmd_spec_columns)

    f = sub.add_parser("fetch", help="蓄積系データ(JVOpen)を期間を指定して取得する")
    f.add_argument("--dataspec", required=True, help="データ種別ID（RACE, DIFF, BLOD …）")
    f.add_argument(
        "--from",
        dest="from_",
        help="読み出し開始ポイント時刻 YYYYMMDDhhmmss（auto で --state の続きから）",
    )
    f.add_argument("--to", help="読み出し終了ポイント時刻 YYYYMMDDhhmmss")
    f.add_argument(
        "--option",
        type=int,
        default=1,
        choices=(1, 2, 3, 4),
        help="1:通常 2:今週 3:セットアップ 4:ダイアログ無しセットアップ",
    )
    f.add_argument("--state", help="最新タイムスタンプを保存する JSON（差分取得用）")
    f.add_argument("--dry-run", action="store_true", help="JVOpen の件数だけ確認して終了")
    _add_jvlink_args(f)
    _add_output_args(f)
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("rt", help="速報系データ(JVRTOpen)を取得する")
    r.add_argument("--dataspec", required=True, help="速報系データ種別ID（0B12, 0B15 …）")
    r.add_argument("--key", required=True, help="要求キー（YYYYMMDD / YYYYMMDDJJKKHHRR など）")
    _add_jvlink_args(r)
    _add_output_args(r)
    r.set_defaults(func=cmd_rt)

    pp = sub.add_parser("parse", help="保存済みの生データを読み込む（JV-Link 不要）")
    pp.add_argument("raw", nargs="+", help="生データのファイル・ディレクトリ・glob")
    _add_output_args(pp)
    pp.set_defaults(func=cmd_parse)

    y = sub.add_parser("sync", help="過去N年ぶんの蓄積系データをまとめて DuckDB に入れる")
    y.add_argument("--years", type=int, default=10, help="何年ぶん遡るか（既定: 10）")
    y.add_argument(
        "--dataspec",
        help="データ種別IDをカンマ区切りで限定（既定: 蓄積系の12種別すべて）",
    )
    y.add_argument(
        "--force-setup",
        action="store_true",
        help="続きからではなく、セットアップ取得をやり直す",
    )
    y.add_argument("--dry-run", action="store_true", help="対象ファイル数だけ確認して終了")
    _add_jvlink_args(y)
    _add_output_args(y)
    y.set_defaults(func=cmd_sync)

    s = sub.add_parser("setup", help="JV-Link の設定ダイアログを開く")
    s.add_argument("--sid", default="UNKNOWN")
    s.add_argument("--save-files", type=int, choices=(0, 1))
    s.set_defaults(func=cmd_setup)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
