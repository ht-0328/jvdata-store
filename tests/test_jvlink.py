"""JVLink ラッパが JVGets を正しく呼ぶことを固定する。

JV-Link は使わない。COM オブジェクトを偽物に差し替えて、渡した引数を記録する。
"""

from __future__ import annotations

import pytest

from jvstore.jvlink import BrokenFileError, JVLink


class FakeCom:
    """JVGets だけを持つ偽の JV-Link。渡されたバッファの長さを記録する。"""

    def __init__(self, record: bytes) -> None:
        self.record = record
        self.buffer_lengths: list[int] = []

    def JVGets(self, buffer, size, filename):  # noqa: N802  （COM のメソッド名に合わせる）
        self.buffer_lengths.append(len(buffer))
        # JV-Link は自分で確保したバイト配列を返す。末尾に余りがあっても切り詰めることを確かめる。
        return len(self.record), memoryview(self.record + b"\x00" * 8), "RAVM20220399.jvd"


def link_with(com: FakeCom) -> JVLink:
    link = JVLink.__new__(JVLink)
    link._com = com
    return link


def test_JVGets_には空のバッファを渡す():
    """JV-Link は渡されたバッファを解放しない。大きなバッファを渡すと1回ごとにその分が残る。"""
    com = FakeCom(b"RA7" + b" " * 100)
    link_with(com).gets()
    assert com.buffer_lengths == [0]


def test_戻り値のバイト数で切り詰める():
    com = FakeCom(b"RA7" + b" " * 100)
    code, data, filename = link_with(com).gets()
    assert code == 103
    assert data == com.record
    assert filename == "RAVM20220399.jvd"


class BrokenCom:
    """JVGets が「ファイルサイズ＝0」を返す偽の JV-Link。"""

    def __init__(self, filename: str, savepath: str = "") -> None:
        self.filename = filename
        self.m_savepath = savepath

    def JVGets(self, buffer, size, filename):  # noqa: N802
        return -402, None, self.filename


def test_壊れたファイルはファイル名を付けて知らせる():
    """仕様書 p.33: 壊れたファイルは JVFiledelete で消し、JVOpen からやり直す。"""
    link = link_with(BrokenCom("H1VM2015129920230808171428.jvd"))
    with pytest.raises(BrokenFileError) as caught:
        list(link.records())
    assert caught.value.code == -402
    assert caught.value.filename == "H1VM2015129920230808171428.jvd"


def test_保存パスの大きさ0のファイルを探せる(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "H1VM2015129920230808171428.jvd").write_bytes(b"")
    (tmp_path / "data" / "H6VM2015129920230808171429.jvd").write_bytes(b"x")
    link = link_with(BrokenCom("", savepath=str(tmp_path)))
    assert link.empty_files() == ["H1VM2015129920230808171428.jvd"]
