"""Formatversion 6 = 5 + mehrere verschraenkte RS-Codewoerter je Zeile
(interleave-Feld, Zeilenbreite unabhaengig von der Alphabetgroesse) und
Presets als Kennung im Header (Alphabet-Modus 2, siehe alphabets.PRESET_IDS)
statt mit allen Zeichen."""
from ..alphabets import PRESET_IDS, PRESETS_BY_ID, Alphabet
from ..header import ALPHABET_MODE_PRESET, HeaderError
from .base import _UINT8_MAX, ByteReader, _check_range
from .v5 import FormatV5


class FormatV6(FormatV5):
    version = 6
    max_interleave = None

    def _pack_alphabet(self, out: bytearray, payload_alphabet: Alphabet, preset: str | None):
        if preset is None or preset == "default":
            super()._pack_alphabet(out, payload_alphabet, preset)
            return
        out.append(ALPHABET_MODE_PRESET)
        out.append(PRESET_IDS[preset])

    def _unpack_alphabet_mode(self, mode: int, rd: ByteReader) -> tuple[str | None, str | None]:
        if mode != ALPHABET_MODE_PRESET:
            return super()._unpack_alphabet_mode(mode, rd)
        preset_id = rd.byte()
        if preset_id not in PRESETS_BY_ID:
            raise HeaderError(f"Unbekannte Preset-Kennung {preset_id} (neuere Programmversion?)")
        return PRESETS_BY_ID[preset_id], None

    def _pack_interleave(self, out: bytearray, interleave: int):
        _check_range("interleave", interleave, _UINT8_MAX)
        out.append(interleave)

    def _unpack_interleave(self, rd: ByteReader) -> int:
        return rd.byte()
