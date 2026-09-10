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
from typing import IO, Callable, Iterable, Iterator

from .layout import LayoutSet, load_layouts
from .record import UNKNOWN_STATS_KEY, FlatLayout
from .store import DuckStore
from .writer import CsvSink

DEFAULT_LAYOUTS = Path(__file__).with_name("resources") / "layouts.json"

#: 未知のレコード種別を種別ごとに数える ``stats`` のキーの接頭辞。
_UNKNOWN_PREFIX = "(未知)"

#: 出力しなかった（``--only`` で外した）レコード種別を表す ``stats`` のキーの接尾辞。
_EXCLUDED_SUFFIX = "(除外)"

#: 途中経過を出すレコード件数の刻み。
_PROGRESS_EVERY = 50000

#: レコード列。1 レコードぶんの生データと、それが入っていたファイル名の対。
RawRecords = Iterator[tuple[bytes, str]]

#: 書き出し先。DuckDB でも CSV でも同じ ``write`` / ``close`` を持つ。
Sink = CsvSink | DuckStore


# --------------------------------------------------------------------- 共通
def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _load_layouts(args: argparse.Namespace) -> LayoutSet:
    path = Path(args.layouts) if getattr(args, "layouts", None) else None
    return load_layouts(path)


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


def _report(stats: Counter[str], sink: Sink) -> None:
    """レコード種別ごとの件数と、書き出し先を出す。"""
    _log("")
    _log("--- 出力結果（表＝レコード種別ごと） ---")
    skipped: list[str] = []
    for key, count in sorted(stats.items()):
        if key.startswith("("):
            continue
        record_id = key.split("(")[0]
        layout = sink.layouts.get(record_id)
        title = layout.title if layout else "?"
        if key.endswith(_EXCLUDED_SUFFIX):
            skipped.append(f"{record_id} {title} {count:,}件")
            continue
        _log(f"  {record_id} {title:<16} {count:>9,} 件")
    if skipped:
        _log(f"  --only 指定により出力しなかった表: {', '.join(skipped)}")
    _report_unknown(stats)
    for path in sink.written_files():
        _log(f"  -> {path}")


def _report_unknown(stats: Counter[str]) -> None:
    """レイアウト定義に無いレコード種別。仕様改訂に気づくために必ず出す。"""
    unknown = stats.get(UNKNOWN_STATS_KEY, 0)
    if not unknown:
        return
    record_ids = sorted(
        key[len(_UNKNOWN_PREFIX) :]
        for key in stats
        if key.startswith(_UNKNOWN_PREFIX)
    )
    _log(f"  未知のレコード種別: {unknown:,} 件 {record_ids}")


# --------------------------------------------------------------------- spec
def cmd_spec_build(args: argparse.Namespace) -> int:
    from .spec_parser import parse_spec_workbook, validate

    xlsx = Path(args.xlsx)
    out = Path(args.out) if args.out else DEFAULT_LAYOUTS
    _log(f"仕様書を読み込み中: {xlsx}")
    parsed = parse_spec_workbook(xlsx)
    broken = 0
    for layout in parsed:
        problems = validate(layout)
        for problem in problems:
            _log(f"  [警告] {problem}")
        broken += bool(problems)
    parsed.dump(out)
    _log(
        f"表 {len(parsed.layouts)} 件、データ種別 {len(parsed.dataspecs)} 件を"
        f"書き出しました: {out}"
    )
    if broken:
        _log(f"[警告] {broken} 表で位置の整合性エラーがあります")
    return 1 if broken and args.strict else 0


def cmd_spec_list(args: argparse.Namespace) -> int:
    layouts = _load_layouts(args)
    print(f"# JV-Data仕様書 {layouts.version} ({layouts.source})")
    print()
    print("## 表（レコード種別）")
    print(f"{'表番号':>6} {'ID':<4} {'表題':<22} {'レコード長':>9} {'カラム数':>8}")
    for layout in sorted(layouts, key=lambda item: int(item.index)):
        flat = FlatLayout(layout)
        print(
            f"{layout.index:>6} {layout.record_id:<4} {layout.title:<22} "
            f"{layout.length:>9,} {len(flat.columns):>8,}"
        )
    if layouts.dataspecs:
        print()
        print("## データ種別ID（--dataspec に指定する値）")
        for spec in layouts.dataspecs.values():
            categories = "/".join(spec.categories) or "-"
            print(f"  {spec.id:<6} {categories:<20} {spec.name}")
            print(f"         収録レコード: {' '.join(spec.record_ids)}")
    return 0


def cmd_spec_columns(args: argparse.Namespace) -> int:
    layouts = _load_layouts(args)
    layout = layouts.get(args.record_id.upper())
    if layout is None:
        _log(f"レコード種別 {args.record_id} は定義にありません")
        return 1
    flat = FlatLayout(layout, keep_separator=args.keep_separator)
    print(
        f"# {layout.record_id} {layout.title} "
        f"（表 {layout.index}／{layout.length} バイト）"
    )
    print(f"{'項番':<6} {'位置':>7} {'長':>5} キー カラム名")
    for column in flat.columns:
        print(
            f"{column.item_no:<6} {column.offset + 1:>7} {column.size:>5} "
            f"{'○' if column.is_key else ' ':<3} {column.name}"
        )
    print(f"\nカラム数: {len(flat.columns):,}")
    return 0


# -------------------------------------------------------------------- 取得
def _build_sink(args: argparse.Namespace, layouts: LayoutSet) -> Sink:
    """出力先を決める。``--out`` を付けたときだけ CSV にする。"""
    only = args.only.split(",") if args.only else None
    if not args.out:
        return DuckStore(
            Path(args.db), layouts, only=only, strip=not args.keep_padding
        )
    outdir = Path(args.out)
    return CsvSink(
        outdir,
        layouts,
        encoding=args.encoding,
        strip=not args.keep_padding,
        keep_separator=args.keep_separator,
        only=only,
        append=args.append,
        unknown_path=outdir / "_unknown_records.txt" if args.save_unknown else None,
    )


class _RawFileWriter:
    """受信した生データを、届いたファイル名ごとに保存する。"""

    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir
        self._files: dict[str, IO[bytes]] = {}

    def write(self, data: bytes, filename: str) -> None:
        name = filename or "unknown.jvd"
        stream = self._files.get(name)
        if stream is None:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            stream = self._files[name] = (self.raw_dir / name).open("wb")
        stream.write(data)

    def close(self) -> None:
        for stream in self._files.values():
            stream.close()
        self._files.clear()


def _consume(
    records: RawRecords,
    args: argparse.Namespace,
    layouts: LayoutSet,
    raw_dir: Path | None,
) -> Counter[str]:
    """レコード列を DuckDB（--out 指定時は CSV）に書き出す。"""
    sink = _build_sink(args, layouts)
    raw_files = _RawFileWriter(raw_dir) if raw_dir is not None else None
    started = time.time()
    written = 0
    try:
        for data, filename in records:
            sink.write(data)
            written += 1
            if raw_files is not None:
                raw_files.write(data, filename)
            if args.limit and written >= args.limit:
                _log(f"--limit {args.limit} に達したので読み込みを打ち切ります")
                break
            if written % _PROGRESS_EVERY == 0:
                _log(f"  {written:,} レコード処理 ({time.time() - started:.0f}秒)")
    finally:
        sink.close()
        if raw_files is not None:
            raw_files.close()
    _log(f"合計 {written:,} レコード / {time.time() - started:.1f} 秒")
    _report(sink.stats, sink)
    return sink.stats


def _open_link(args: argparse.Namespace):
    from .jvlink import JVLink

    link = JVLink(args.sid)
    link.init()
    if args.save_files is not None:
        link.set_save_flag(1 if args.save_files else 0)
    return link


def _link_records(link, on_file: Callable[[str], None] | None = None) -> RawRecords:
    """JV-Link から届くレコードを ``_consume`` が扱う形にそろえる。"""
    for record in link.records(on_file=on_file):
        yield record.data, record.filename


def _raw_dir_of(args: argparse.Namespace) -> Path | None:
    return Path(args.save_raw) if args.save_raw else None


def cmd_fetch(args: argparse.Namespace) -> int:
    from .jvlink import JVLinkError

    layouts = _load_layouts(args)
    state_path = Path(args.state) if args.state else None
    fromtime = _resolve_fromtime(args, state_path)
    if fromtime is None:
        return 2

    link = _open_link(args)
    try:
        _log(
            f"JVOpen dataspec={args.dataspec} fromtime={fromtime} "
            f"option={args.option}"
        )
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
            on_progress=lambda done, total: _log(f"  ダウンロード {done}/{total}"),
        )
        _consume(
            _link_records(link, on_file=lambda name: _log(f"  読込: {name}")),
            args,
            layouts,
            _raw_dir_of(args),
        )
        if not args.limit:
            _state_save(state_path, args.dataspec, result.last_file_timestamp)
        return 0
    except JVLinkError as error:
        _log(str(error))
        return 1
    finally:
        link.close()


def _resolve_fromtime(
    args: argparse.Namespace, state_path: Path | None
) -> str | None:
    """読み出し開始ポイント時刻。``auto`` なら前回の続きから。決まらなければ None。"""
    fromtime = args.from_
    if fromtime in (None, "", "auto"):
        saved = _state_load(state_path).get(args.dataspec, {})
        fromtime = saved.get("last_file_timestamp", "")
        if not fromtime:
            _log("前回取得時刻が不明です。--from に YYYYMMDDhhmmss を指定してください。")
            return None
        _log(f"前回の続き（{fromtime}）から取得します")
    if args.to:
        return f"{fromtime}-{args.to}"
    return fromtime


def cmd_rt(args: argparse.Namespace) -> int:
    from .jvlink import JVLinkError

    layouts = _load_layouts(args)
    link = _open_link(args)
    try:
        _log(f"JVRTOpen dataspec={args.dataspec} key={args.key}")
        link.rt_open(args.dataspec, args.key)
        _consume(_link_records(link), args, layouts, _raw_dir_of(args))
        return 0
    except JVLinkError as error:
        _log(str(error))
        return 1
    finally:
        link.close()


def cmd_parse(args: argparse.Namespace) -> int:
    layouts = _load_layouts(args)
    _consume(_saved_records(args.raw), args, layouts, None)
    return 0


def _saved_records(patterns: Iterable[str]) -> RawRecords:
    """保存済みの生データを1行1レコードとして読む。"""
    for pattern in patterns:
        for path in sorted(_expand(pattern)):
            _log(f"  読込: {path}")
            with path.open("rb") as stream:
                for line in stream:
                    record = line.rstrip(b"\r\n")
                    if record:
                        yield record, path.name


def _expand(pattern: str) -> Iterable[Path]:
    path = Path(pattern)
    if path.exists():
        return [path] if path.is_file() else sorted(path.glob("*"))
    return sorted(Path().glob(pattern))


# --------------------------------------------------------------------- sync
def cmd_sync(args: argparse.Namespace) -> int:
    """過去N年ぶんの蓄積系データを、種別を順に回してまとめて DuckDB へ入れる。

    取得の本体は :mod:`jvstore.sync` にある。
    jvstore の Web 画面もこのコマンドを呼ぶので、挙動がずれない。
    """
    from .sync import SYNC_DATASPECS, sync

    specs = (
        [name.strip().upper() for name in args.dataspec.split(",")]
        if args.dataspec
        else [name for name, _ in SYNC_DATASPECS]
    )
    result = sync(
        Path(args.db),
        years=args.years,
        dataspecs=specs,
        log=_log,
        layouts=_load_layouts(args),
        link_factory=lambda: _open_link(args),
        only=args.only.split(",") if args.only else None,
        dry_run=args.dry_run,
        force_setup=args.force_setup,
    )

    _log("")
    _log("--- テーブルごとの行数 ---")
    for table, count in result.counts.items():
        _log(f"  {table:<28} {count:>12,}")
    if result.failed:
        _log(f"取得できなかったデータ種別: {', '.join(result.failed)}")
        return 1
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    link = _open_link(args)
    _log("JV-Link の設定ダイアログを開きます（利用キーの登録・利用規約への同意）")
    link.set_ui_properties()
    return 0


# --------------------------------------------------------------------- 引数
def _add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db", default="jvdata.duckdb", help="DuckDB の保存先（既定: jvdata.duckdb）"
    )
    parser.add_argument("--out", help="CSV 出力先ディレクトリ（指定したときだけ CSV にする）")
    parser.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="CSV の文字コード（既定: utf-8-sig／Excel 用は cp932 も可）",
    )
    parser.add_argument(
        "--only", help="出力するレコード種別IDをカンマ区切りで限定（例: RA,SE）"
    )
    parser.add_argument("--append", action="store_true", help="既存 CSV に追記する")
    parser.add_argument(
        "--keep-padding", action="store_true", help="項目値の右側の空白を削らない"
    )
    parser.add_argument(
        "--keep-separator", action="store_true", help="レコード区切(CR/LF)列も出力する"
    )
    parser.add_argument(
        "--save-unknown", action="store_true", help="未知のレコードを保存する"
    )
    parser.add_argument("--limit", type=int, help="読み込むレコード数の上限（動作確認用）")
    parser.add_argument(
        "--layouts", help="レイアウト定義 JSON のパス（既定: 同梱の layouts.json）"
    )


def _add_jvlink_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sid", default="UNKNOWN", help="JVInit に渡すソフトウェアID")
    parser.add_argument("--save-raw", help="受信した生データをこのディレクトリに保存する")
    parser.add_argument(
        "--save-files",
        type=int,
        choices=(0, 1),
        help="JVSetSaveFlag: ダウンロードした JV-Data ファイルを残すか",
    )


def _add_spec_parsers(subparsers: argparse._SubParsersAction) -> None:
    spec = subparsers.add_parser("spec", help="仕様書（レイアウト定義）の生成・参照")
    spec_commands = spec.add_subparsers(dest="spec_command", required=True)

    build = spec_commands.add_parser(
        "build", help="JV-Data仕様書 xlsx からレイアウト定義を生成"
    )
    build.add_argument("--xlsx", required=True, help="JV-Data仕様書_x.x.x.x.xlsx のパス")
    build.add_argument("--out", help=f"出力先 JSON（既定: {DEFAULT_LAYOUTS}）")
    build.add_argument(
        "--strict", action="store_true", help="整合性エラーがあれば異常終了する"
    )
    build.set_defaults(func=cmd_spec_build)

    listing = spec_commands.add_parser("list", help="表とデータ種別IDの一覧")
    listing.add_argument("--layouts")
    listing.set_defaults(func=cmd_spec_list)

    columns = spec_commands.add_parser("columns", help="表のカラム定義を表示")
    columns.add_argument("record_id", help="レコード種別ID（RA, SE, UM …）")
    columns.add_argument("--keep-separator", action="store_true")
    columns.add_argument("--layouts")
    columns.set_defaults(func=cmd_spec_columns)


def _add_fetch_parser(subparsers: argparse._SubParsersAction) -> None:
    fetch = subparsers.add_parser(
        "fetch", help="蓄積系データ(JVOpen)を期間を指定して取得する"
    )
    fetch.add_argument(
        "--dataspec", required=True, help="データ種別ID（RACE, DIFF, BLOD …）"
    )
    fetch.add_argument(
        "--from",
        dest="from_",
        help="読み出し開始ポイント時刻 YYYYMMDDhhmmss（auto で --state の続きから）",
    )
    fetch.add_argument("--to", help="読み出し終了ポイント時刻 YYYYMMDDhhmmss")
    fetch.add_argument(
        "--option",
        type=int,
        default=1,
        choices=(1, 2, 3, 4),
        help="1:通常 2:今週 3:セットアップ 4:ダイアログ無しセットアップ",
    )
    fetch.add_argument("--state", help="最新タイムスタンプを保存する JSON（差分取得用）")
    fetch.add_argument(
        "--dry-run", action="store_true", help="JVOpen の件数だけ確認して終了"
    )
    _add_jvlink_args(fetch)
    _add_output_args(fetch)
    fetch.set_defaults(func=cmd_fetch)


def _add_rt_parser(subparsers: argparse._SubParsersAction) -> None:
    realtime = subparsers.add_parser("rt", help="速報系データ(JVRTOpen)を取得する")
    realtime.add_argument(
        "--dataspec", required=True, help="速報系データ種別ID（0B12, 0B15 …）"
    )
    realtime.add_argument(
        "--key", required=True, help="要求キー（YYYYMMDD / YYYYMMDDJJKKHHRR など）"
    )
    _add_jvlink_args(realtime)
    _add_output_args(realtime)
    realtime.set_defaults(func=cmd_rt)


def _add_parse_parser(subparsers: argparse._SubParsersAction) -> None:
    parse = subparsers.add_parser(
        "parse", help="保存済みの生データを読み込む（JV-Link 不要）"
    )
    parse.add_argument("raw", nargs="+", help="生データのファイル・ディレクトリ・glob")
    _add_output_args(parse)
    parse.set_defaults(func=cmd_parse)


def _add_sync_parser(subparsers: argparse._SubParsersAction) -> None:
    sync = subparsers.add_parser(
        "sync", help="過去N年ぶんの蓄積系データをまとめて DuckDB に入れる"
    )
    sync.add_argument("--years", type=int, default=10, help="何年ぶん遡るか（既定: 10）")
    sync.add_argument(
        "--dataspec",
        help="データ種別IDをカンマ区切りで限定（既定: 蓄積系の12種別すべて）",
    )
    sync.add_argument(
        "--force-setup",
        action="store_true",
        help="続きからではなく、セットアップ取得をやり直す",
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="対象ファイル数だけ確認して終了"
    )
    _add_jvlink_args(sync)
    _add_output_args(sync)
    sync.set_defaults(func=cmd_sync)


def _add_setup_parser(subparsers: argparse._SubParsersAction) -> None:
    setup = subparsers.add_parser("setup", help="JV-Link の設定ダイアログを開く")
    setup.add_argument("--sid", default="UNKNOWN")
    setup.add_argument("--save-files", type=int, choices=(0, 1))
    setup.set_defaults(func=cmd_setup)


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.server import serve
    serve(Path(args.db), args.port, args.open)
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    """操作パネルを開く。サーバーは `serve` と同じ引数で別プロセスとして起動する。"""
    import tempfile

    from .web.local_server import LocalServer, console_python
    from .web.panel import run_panel

    db = Path(args.db).resolve()
    command = [console_python(), "-m", "jvstore.cli", "serve",
               "--db", str(db), "--port", str(args.port)]
    log_path = Path(tempfile.gettempdir()) / f"jvstore-serve-{args.port}.log"
    run_panel(LocalServer("jvdata-store", args.port, command, Path.cwd(), log_path),
              "jvdata-store")
    return 0


def _add_screen_parsers(subparsers: argparse._SubParsersAction) -> None:
    screen = subparsers.add_parser("serve", help="データ取得・管理画面を開く")
    panel = subparsers.add_parser(
        "panel", help="画面の起動・停止・開き直しを行う操作パネルを開く（run.bat はこれを使う）")
    for parser in (screen, panel):
        parser.add_argument("--db", type=Path, default=Path("jvdata.duckdb"))
        parser.add_argument("--port", type=int, default=8766)
    screen.add_argument("--open", action="store_true", help="ブラウザを開く")
    screen.set_defaults(func=cmd_serve)
    panel.set_defaults(func=cmd_panel)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jvstore",
        description=(
            "JRA-VAN Data Lab.(JV-Link) のデータを JV-Data仕様書の表単位で DuckDB に貯める"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_spec_parsers(subparsers)
    _add_fetch_parser(subparsers)
    _add_rt_parser(subparsers)
    _add_parse_parser(subparsers)
    _add_sync_parser(subparsers)
    _add_setup_parser(subparsers)
    _add_screen_parsers(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
