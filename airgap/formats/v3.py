"""Formatversion 3: Ausgangsbasis der Vererbungskette.

- kein flags-Byte, Nutzdaten nie verwuerfelt
- Alphabet: Modus 0 = Preset 'default', Modus 1 = Zeichenfolge im Header
  (auch fuer alle anderen Presets)
- genau ein RS-Codewort je Nutzdatenzeile (kein interleave-Feld)
- Zeilen-CRC erfasst nur das untere Byte jedes Symbols
"""
import struct

from ..alphabets import Alphabet
from ..header import ALPHABET_MODE_CUSTOM, ALPHABET_MODE_DEFAULT, HeaderError
from .base import ByteReader, Format


class FormatV3(Format):
    version = 3
    max_interleave = 1
    supports_scrambling = False

    def crc_is_wide(self, bits_per_symbol: int) -> bool:
        return False

    def _pack_flags(self, out: bytearray, scrambled: bool):
        if scrambled:
            raise ValueError(f"Formatversion {self.version} kennt keine Verwuerfelung")

    def _unpack_flags(self, rd: ByteReader) -> bool:
        return False

    def _pack_alphabet(self, out: bytearray, payload_alphabet: Alphabet, preset: str | None):
        if preset == "default":
            out.append(ALPHABET_MODE_DEFAULT)
            return
        # utf-8 statt ascii: ein Custom-Alphabet darf bewusst ueber 7-Bit-ASCII
        # hinausgehen (siehe alphabets.py). Laengenfeld ist die BYTE-Laenge,
        # nicht die Zeichenanzahl - deshalb 2 Byte statt 1 (koennte bei vielen
        # Mehrbyte-Zeichen sonst ueberlaufen).
        chars = payload_alphabet.chars.encode("utf-8")
        out.append(ALPHABET_MODE_CUSTOM)
        out.extend(struct.pack(">H", len(chars)))
        out.extend(chars)

    def _unpack_alphabet(self, rd: ByteReader) -> tuple[str | None, str | None]:
        return self._unpack_alphabet_mode(rd.byte(), rd)

    def _unpack_alphabet_mode(self, mode: int, rd: ByteReader) -> tuple[str | None, str | None]:
        """Liest die zum Alphabet-Modus `mode` gehoerenden Felder."""
        if mode == ALPHABET_MODE_DEFAULT:
            return "default", None
        if mode == ALPHABET_MODE_CUSTOM:
            (alen,) = struct.unpack(">H", rd.take(2))
            return None, rd.take(alen).decode("utf-8")
        raise HeaderError(f"Unbekannter Alphabet-Modus {mode}")

    def _pack_interleave(self, out: bytearray, interleave: int):
        if interleave != 1:
            raise ValueError(f"Formatversion {self.version} erlaubt nur ein Codewort je Zeile")

    def _unpack_interleave(self, rd: ByteReader) -> int:
        return 1
