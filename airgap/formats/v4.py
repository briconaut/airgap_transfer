"""Formatversion 4 = 3 + flags-Byte (Bit 0: Nutzdaten verwuerfelt, siehe
scramble.py)."""
from ..header import FLAG_SCRAMBLED
from .base import ByteReader
from .v3 import FormatV3


class FormatV4(FormatV3):
    version = 4
    supports_scrambling = True

    def _pack_flags(self, out: bytearray, scrambled: bool):
        out.append(FLAG_SCRAMBLED if scrambled else 0)

    def _unpack_flags(self, rd: ByteReader) -> bool:
        return bool(rd.byte() & FLAG_SCRAMBLED)
