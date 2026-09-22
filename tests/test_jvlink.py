"""JVLink ラッパが JVGets を正しく呼ぶことを固定する。

JV-Link は使わない。COM オブジェクトを偽物に差し替えて、渡した引数を記録する。
"""

from __future__ import annotations

from jvstore.jvlink import JVLink


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
