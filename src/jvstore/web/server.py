"""JRA-VAN データ取得・閲覧用のローカル HTTP サーバー。"""

from __future__ import annotations

import contextlib
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import duckdb

from ..sync import SYNC_DATASPECS
from .db_lock import DatabaseLock
from .tables import TableBrowser
from .tasks import Task

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 8766


class Backend:
    def __init__(self, db: Path):
        self.db = db.resolve()
        self.task = Task()
        self._lock = DatabaseLock(self.db)

    def info(self):
        return {"app": "jvdata-store", "db": str(self.db)}

    def dataspecs(self):
        return [{"id": name, "title": title} for name, title in SYNC_DATASPECS]

    def fetch_history(self, years: int, dataspecs: str = "", *, force_setup=False):
        args = ["sync", "--years", str(years), "--db", str(self.db)]
        label = f"過去 {years} 年ぶんの取得"
        if dataspecs:
            args += ["--dataspec", dataspecs]
            label += f"（{dataspecs}）"
        if force_setup:
            args += ["--force-setup"]
            label += "（期間を広げて再取得）"
        return self.task.start(label, [[sys.executable, "-m", "jvstore.cli", *args]],
                               Path.cwd(), db_lock=self._lock)

    def jvlink_setup(self):
        return self.task.start("JV-Link設定", [[sys.executable, "-m", "jvstore.cli", "setup"]],
                               Path.cwd(), db_lock=self._lock)

    @contextlib.contextmanager
    def _db(self):
        # 表一覧と行表示の並行リクエストは順番に処理する。取得中だけ即座に返す。
        if self.task.snapshot()["running"] or not self._lock.acquire(timeout=15):
            raise BlockingIOError("取得または予想を実行中です。終了後に一覧を更新してください。")
        try:
            yield
        finally:
            self._lock.release()

    def history_tables(self, date_from=None, date_to=None):
        with self._db():
            if not self.db.exists():
                return {"db": str(self.db), "ready": False, "tables": []}
            with duckdb.connect(str(self.db), read_only=True) as con:
                return {"db": str(self.db), "ready": True, "tables": [
                    vars(table) for table in TableBrowser(con).list_tables(date_from, date_to)
                ]}

    def history_rows(self, name: str, **options):
        with self._db():
            if not self.db.exists():
                raise FileNotFoundError("データはまだ取得されていません。")
            with duckdb.connect(str(self.db), read_only=True) as con:
                return TableBrowser(con).read(name, **options)


def _one(query, name, default=""):
    return query.get(name, [default])[0]


def make_handler(backend: Backend):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status=200):
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status)

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send((STATIC / "index.html").read_bytes(),
                                      "text/html; charset=utf-8")
                if url.path == "/api/info":
                    return self._json(backend.info())
                if url.path == "/api/task":
                    return self._json(backend.task.snapshot())
                if url.path == "/api/dataspecs":
                    return self._json({"dataspecs": backend.dataspecs()})
                if url.path == "/api/history/tables":
                    return self._json(backend.history_tables(
                        _one(query, "from") or None, _one(query, "to") or None))
                if url.path == "/api/history/rows":
                    return self._json(backend.history_rows(
                        _one(query, "name"), limit=int(_one(query, "limit", "50")),
                        offset=int(_one(query, "offset", "0")),
                        column_offset=int(_one(query, "column_offset", "0")),
                        date_from=_one(query, "from") or None,
                        date_to=_one(query, "to") or None))
                return self._json({"error": "not found"}, 404)
            except BlockingIOError as error:
                return self._json({"error": str(error)}, 409)
            except (ValueError, LookupError) as error:
                return self._json({"error": str(error)}, 400)
            except Exception as error:  # noqa: BLE001  画面に理由を返し、サーバは止めない
                return self._json({"error": str(error)}, 500)

        def do_POST(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/api/jvlink-setup":
                    return self._json({"started": backend.jvlink_setup()})
                if url.path != "/api/history/fetch":
                    return self._json({"error": "not found"}, 404)
                years = int(_one(query, "years", "10"))
                if not 1 <= years <= 40:
                    raise ValueError("年数は1〜40の整数で指定してください。")
                requested = _one(query, "dataspec")
                known = {spec["id"] for spec in backend.dataspecs()}
                if requested and not set(requested.split(",")) <= known:
                    raise ValueError("知らないデータ種別です。")
                force = _one(query, "force_setup", "0")
                if force not in ("0", "1"):
                    raise ValueError("取得方法の指定が不正です。")
                return self._json({"started": backend.fetch_history(
                    years, requested, force_setup=force == "1")})
            except ValueError as error:
                return self._json({"error": str(error)}, 400)
            except Exception as error:  # noqa: BLE001  画面に理由を返し、サーバは止めない
                return self._json({"error": str(error)}, 500)

    return Handler


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False


def serve(db: Path, port: int = DEFAULT_PORT, open_browser: bool = False):
    backend = Backend(db)
    url = f"http://127.0.0.1:{port}/"
    try:
        server = _Server(("127.0.0.1", port), make_handler(backend))
    except OSError as error:
        # ダブルクリックの繰り返しでは既存画面を開く。別の保存先は取り違えない。
        try:
            with urlopen(url + "api/info", timeout=2) as response:
                info = json.load(response)
            same = info.get("app") == "jvdata-store" and Path(info["db"]).resolve() == backend.db
        except Exception:  # noqa: BLE001  応答が読めなければ別の画面とみなす
            same = False
        if not same:
            raise SystemExit(f"ポート {port} は別の画面で使用中です。--port で変更してください。") from error
        if open_browser:
            webbrowser.open(url)
        print(f"起動済みの画面: {url}")
        return
    print(f"JRA-VAN データ取得・管理: {url}", flush=True)
    print(f"保存先: {backend.db}\n終了するには Ctrl+C", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
