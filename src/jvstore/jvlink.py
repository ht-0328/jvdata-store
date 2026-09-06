"""JV-Link (COM: ``JVDTLab.JVLink``) の薄いラッパ。

JRA-VAN Data Lab. のデータは JV-Link 経由でしか取得できない。ここでは
インターフェース仕様書 4.9.0.1 の呼び出し順序をそのままなぞる:

    JVInit → JVOpen / JVRTOpen → (JVStatus でDL進捗) → JVGets を EOF まで → JVClose

JVGets は 1 呼び出しで 1 レコード分の **bytes** を返す。固定長レコードを
バイト位置で切り出すため、str ではなく bytes のまま扱うこと。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterator

__all__ = [
    "JVLink",
    "JVLinkError",
    "OpenResult",
    "ReadRecord",
    "describe_error",
]

# インターフェース仕様書「３．コード表」より。メソッド共通の主要コードのみ。
_ERRORS: dict[int, str] = {
    -1: "該当データ無し（または最新バージョンのダウンロードが選択された）",
    -2: "セットアップダイアログでキャンセルが押された",
    -111: "dataspec パラメータが不正",
    -112: "fromtime パラメータが不正（読み出し開始ポイント時刻不正）",
    -113: "fromtime パラメータが不正（読み出し終了ポイント時刻不正）",
    -114: "key パラメータが不正",
    -115: "option パラメータが不正",
    -116: "dataspec と option の組み合わせが不正",
    -118: "filepath パラメータが不正",
    -201: "JVInit が行われていない",
    -202: "前回の JVOpen/JVRTOpen に対して JVClose が呼ばれていない（オープン中）",
    -203: "JVOpen が行われていない",
    -211: "レジストリ内容が不正",
    -301: "認証エラー（利用キーが正しくない／複数マシンでの同一キー使用）",
    -302: "利用キーの有効期限切れ",
    -303: "利用キーが設定されていない（JV-Link の設定画面で利用キーを登録してください）",
    -305: "利用規約に同意していない（JV-Link の設定画面で同意してください）",
    -401: "JV-Link 内部エラー",
    -402: "ダウンロードしたファイルが異常（ファイルサイズ＝0）",
    -403: "ダウンロードしたファイルが異常（データ内容）",
    -411: "サーバーエラー（HTTP 404 NotFound）",
    -412: "サーバーエラー（HTTP 403 Forbidden）",
    -413: "サーバーエラー（HTTP 200,403,404 以外）",
    -421: "サーバーエラー（サーバーの応答が不正）",
    -431: "サーバーエラー（サーバーアプリケーション内部エラー）",
    -501: "セットアップ処理においてスタートキットが無効",
    -502: "ダウンロード失敗（通信エラーやディスクエラーなど）",
    -503: "ファイルが見つからない",
    -504: "サーバーメンテナンス中",
}


def describe_error(code: int) -> str:
    return _ERRORS.get(code, "未定義のエラーコード")


class JVLinkError(RuntimeError):
    """JV-Link が負の戻り値を返したときの例外。"""

    def __init__(self, func: str, code: int) -> None:
        super().__init__(f"{func} エラー: {code} ({describe_error(code)})")
        self.func = func
        self.code = code


@dataclass(slots=True)
class OpenResult:
    """JVOpen の出力パラメータ。"""

    read_count: int
    """読み込み対象の全ファイル数。"""
    download_count: int
    """うちサーバーからのダウンロードが必要なファイル数。"""
    last_file_timestamp: str
    """対象ファイル中で最も新しいタイムスタンプ。次回 JVOpen の fromtime に使う。"""


@dataclass(slots=True)
class ReadRecord:
    """JVGets が返した 1 レコード。"""

    data: bytes
    filename: str
    """そのレコードが入っていた JV-Data ファイル名。"""


class JVLink:
    """JV-Link COM オブジェクトのラッパ。``with`` で使うと確実に JVClose される。"""

    PROGID = "JVDTLab.JVLink"
    BUFFER_SIZE = 200_000  # 最長レコード(H6: 102,890バイト)より十分大きく取る

    def __init__(self, sid: str = "UNKNOWN") -> None:
        import pythoncom  # noqa: F401  （COM を使うスレッドで初期化しておく）
        import win32com.client

        pythoncom.CoInitialize()
        try:
            self._com = win32com.client.Dispatch(self.PROGID)
        except Exception:
            pythoncom.CoUninitialize()
            raise
        self.sid = sid
        self._opened = False
        self._disposed = False

    # ------------------------------------------------------------------ 基本
    def init(self) -> None:
        """JVInit。他のメソッドより先に必ず 1 回呼ぶ。"""
        ret = int(self._com.JVInit(self.sid))
        if ret != 0:
            raise JVLinkError("JVInit", ret)

    def set_ui_properties(self) -> None:
        """JV-Link の設定ダイアログ（利用キー登録・利用規約同意）を開く。"""
        self._com.JVSetUIProperties()

    def set_save_flag(self, flag: int) -> None:
        """ダウンロードした JV-Data ファイルをローカルに残すか（1:残す 0:残さない）。"""
        self._com.JVSetSaveFlag(flag)

    def close(self) -> None:
        if self._opened:
            try:
                self._com.JVClose()
            finally:
                self._opened = False

    def cancel(self) -> None:
        self._com.JVCancel()

    def dispose(self) -> None:
        """Release COM on the creating thread when this instance will not be reused."""
        if self._disposed:
            return
        try:
            self.close()
        finally:
            import pythoncom

            self._com = None
            self._disposed = True
            pythoncom.CoUninitialize()

    def file_delete(self, filename: str) -> None:
        ret = int(self._com.JVFiledelete(filename))
        if ret != 0:
            raise JVLinkError("JVFiledelete", ret)

    def __enter__(self) -> "JVLink":
        self.init()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 取得
    def open(self, dataspec: str, fromtime: str, option: int = 1) -> OpenResult:
        """蓄積系データの取得要求（JVOpen）。

        option は 1:通常 2:今週 3:セットアップ 4:ダイアログ無しセットアップ。
        戻り値 -1（該当データ無し）は例外にせず read_count=0 として返す。
        """
        ret = self._com.JVOpen(dataspec, fromtime, int(option), 0, 0, "")
        code = int(ret[0]) if isinstance(ret, (list, tuple)) else int(ret)
        if code == -1:
            self._opened = True  # -1 でも JVClose は必要
            return OpenResult(0, 0, "")
        if code != 0:
            raise JVLinkError("JVOpen", code)
        self._opened = True
        return OpenResult(
            read_count=int(ret[1] or 0),
            download_count=int(ret[2] or 0),
            last_file_timestamp=str(ret[3] or ""),
        )

    def rt_open(self, dataspec: str, key: str) -> None:
        """速報系データの取得要求（JVRTOpen）。dataspec は 4 桁固定、key は提供単位に応じて指定。"""
        code = int(self._com.JVRTOpen(dataspec, key))
        if code == -1:
            self._opened = True
            return
        if code != 0:
            raise JVLinkError("JVRTOpen", code)
        self._opened = True

    def status(self) -> int:
        """ダウンロード済みファイル数（JVStatus）。"""
        code = int(self._com.JVStatus())
        if code < 0:
            raise JVLinkError("JVStatus", code)
        return code

    def skip(self) -> None:
        """読み込み中のファイルを読み飛ばす（JVSkip）。"""
        self._com.JVSkip()

    def gets(self) -> tuple[int, bytes, str]:
        """JVGets を 1 回呼ぶ。戻り値は (コード, データ, ファイル名)。

        コードは >0:読み込んだバイト数 / 0:EOF / -1:ファイル切り替わり / -3:ダウンロード中。
        """
        buff = bytearray(self.BUFFER_SIZE)
        ret, mem, fname = self._com.JVGets(buff, self.BUFFER_SIZE, bytearray())
        code = int(ret)
        # 戻り値はバッファにセットされたデータのサイズ。バッファ全体が返るので
        # 必ず code バイトで切り詰める（残りは未初期化の 0x00 が並ぶ）。
        data = mem.tobytes()[:code] if code > 0 and mem is not None else b""
        return code, data, str(fname or "")

    def wait_download(
        self,
        result: OpenResult,
        on_progress: Callable[[int, int], None] | None = None,
        interval: float = 0.5,
    ) -> None:
        """ダウンロード完了まで JVStatus を監視する。"""
        if result.download_count <= 0:
            return
        while True:
            done = self.status()
            if on_progress:
                on_progress(done, result.download_count)
            if done >= result.download_count:
                return
            time.sleep(interval)

    def records(
        self,
        on_file: Callable[[str], None] | None = None,
        retry_interval: float = 1.0,
    ) -> Iterator[ReadRecord]:
        """EOF まで JVGets を繰り返し、1 レコードずつ返すイテレータ。"""
        while True:
            code, data, fname = self.gets()
            if code > 0:
                yield ReadRecord(data, fname)
            elif code == -1:
                # ファイル切り替わり。エラーではないので読み込みを続ける。
                if on_file:
                    on_file(fname)
            elif code == -3:
                # 読み出そうとするファイルがまだダウンロード中。
                time.sleep(retry_interval)
            elif code == 0:
                return
            else:
                raise JVLinkError("JVGets", code)
