"""
Gemeinsames Geruest aller Formatversionen (Header-Rohbytes, Zeilen-CRC,
Zeilenlayout). Die konkreten Versionen (v3.py ... v6.py) erben linear
voneinander (V4(V3), V5(V4), ...) und ueberschreiben nur die Hooks, in denen
sie sich von ihrer Vorgaengerin unterscheiden - eine neue Version ist damit
eine Klasse mit wenigen Overrides plus ein Eintrag in der Registry
(formats/__init__.py).

Feldreihenfolge der Header-Rohbytes (Details siehe header.py):
    magic | [flags] | alphabet | text_encoding | document_id | page_count |
    page_number | page_checksum | block_k | block_r | [interleave] |
    total_file_size | sha256 | ln_width | filename_len | filename
Die eckig geklammerten Felder und die Kodierung von 'alphabet' sind
versionsabhaengig (Hooks _pack_*/_unpack_*).
"""
import struct

from ..alphabets import Alphabet
from ..framing import Layout, compute_layout
from ..header import (
    DOCUMENT_ID_WIDTH, MAX_PAGE_COUNT, SHA256_WIDTH, TEXT_ENCODING_TO_CODEC, HeaderError, HeaderMeta,
)

_UINT8_MAX = 0xFF
_UINT16_MAX = 0xFFFF
_UINT64_MAX = 0xFFFF_FFFF_FFFF_FFFF


class ByteReader:
    """Liest Header-Rohbytes der Reihe nach; zu kurze Daten -> HeaderError."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise HeaderError("Header-Rohbytes zu kurz/beschaedigt")
        self.pos += n
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]


def _check_range(name: str, value: int, maximum: int):
    if not 0 <= value <= maximum:
        raise ValueError(f"{name}={value} liegt ausserhalb des Header-Feldbereichs 0..{maximum}")


def truncate_utf8(text: str, max_bytes: int) -> bytes:
    """UTF-8-Kodierung von `text`, auf hoechstens `max_bytes` gekuerzt, ohne
    ein Mehrbyte-Zeichen zu zerschneiden (sonst waere der Name beim Lesen
    kein gueltiges UTF-8 mehr)."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")


class Format:
    """Basisklasse einer Formatversion. Unterklassen setzen `version` und
    implementieren die Hooks; pack_header/unpack_header selbst sind fuer alle
    Versionen gleich."""

    version: int
    # Hoechstzahl verschraenkter RS-Codewoerter je Nutzdatenzeile (None = beliebig)
    max_interleave: int | None
    # Ob das flags-Byte (und damit Verwuerfelung) existiert
    supports_scrambling: bool

    # --- Zeilen-CRC / Layout ------------------------------------------------

    def crc_is_wide(self, bits_per_symbol: int) -> bool:
        """Ob das Zeilen-CRC volle Symbole mit 2 Byte erfasst (siehe
        framing.symbols_crc32)."""
        raise NotImplementedError

    def compute_layout(self, width: int, total_data_symbols: int, payload_alpha: Alphabet,
                       header_alpha: Alphabet, redundancy_pct: float | None,
                       parity_symbols: int | None, clamp_width: bool = False) -> Layout:
        """framing.compute_layout mit der Interleave-Grenze dieser Version.
        clamp_width: eine fuer diese Version zu grosse Breite auf das Maximum
        kuerzen statt ValueError (siehe Layout.width_clamped)."""
        return compute_layout(width, total_data_symbols, payload_alpha, header_alpha,
                              redundancy_pct, parity_symbols, max_interleave=self.max_interleave,
                              clamp_width=clamp_width)

    # --- Header-Rohbytes ----------------------------------------------------

    def pack_header(self, payload_alphabet: Alphabet, preset: str | None, k: int, r: int,
                    interleave: int, total_file_size: int, sha256_digest: bytes, ln_width: int,
                    filename: str, document_id: bytes, page_count: int, page_number: int,
                    page_checksum: int, text_encoding: int, scrambled: bool = False) -> bytes:
        """preset: Preset-Name oder None fuer ein Custom-Alphabet (dann werden
        die Zeichen von payload_alphabet gespeichert)."""
        if text_encoding not in TEXT_ENCODING_TO_CODEC:
            raise ValueError(f"text_encoding muss eines von {tuple(TEXT_ENCODING_TO_CODEC)} sein")
        if len(document_id) != DOCUMENT_ID_WIDTH:
            raise ValueError(f"document_id muss {DOCUMENT_ID_WIDTH} Byte lang sein")
        if len(sha256_digest) != SHA256_WIDTH:
            raise ValueError(f"sha256_digest muss {SHA256_WIDTH} Byte lang sein")
        if page_count > MAX_PAGE_COUNT:
            raise ValueError(
                f"{page_count} Seiten passen nicht ins Header-Feld (maximal {MAX_PAGE_COUNT}) - "
                f"--lines/--columns vergroessern"
            )
        _check_range("page_number", page_number, _UINT16_MAX)
        _check_range("k", k, _UINT16_MAX)
        _check_range("r", r, _UINT16_MAX)
        _check_range("total_file_size", total_file_size, _UINT64_MAX)
        _check_range("ln_width", ln_width, _UINT8_MAX)

        out = bytearray()
        out.append(self.version)
        self._pack_flags(out, scrambled)
        self._pack_alphabet(out, payload_alphabet, preset)
        out.append(text_encoding)
        out.extend(document_id)
        out.extend(struct.pack(">HHIHH", page_count, page_number, page_checksum & 0xFFFFFFFF, k, r))
        self._pack_interleave(out, interleave)
        out.extend(struct.pack(">Q", total_file_size))
        out.extend(sha256_digest)
        out.append(ln_width)
        name_bytes = truncate_utf8(filename, _UINT8_MAX)
        out.append(len(name_bytes))
        out.extend(name_bytes)
        return bytes(out)

    def unpack_header(self, data: bytes) -> HeaderMeta:
        """data: vollstaendige Header-Rohbytes inkl. magic-Byte (das zu dieser
        Version passen muss, siehe formats.unpack_header)."""
        rd = ByteReader(data)
        if rd.byte() != self.version:
            raise HeaderError(f"Header gehoert nicht zu Formatversion {self.version}")
        try:
            scrambled = self._unpack_flags(rd)
            preset, alphabet_chars = self._unpack_alphabet(rd)
            text_encoding = rd.byte()
            document_id = rd.take(DOCUMENT_ID_WIDTH)
            page_count, page_number, page_checksum, k, r = struct.unpack(">HHIHH", rd.take(12))
            interleave = self._unpack_interleave(rd)
            (total_file_size,) = struct.unpack(">Q", rd.take(8))
            sha256_digest = rd.take(SHA256_WIDTH)
            ln_width = rd.byte()
            filename = rd.take(rd.byte()).decode("utf-8")
        except UnicodeDecodeError as e:
            raise HeaderError(f"Header enthaelt ungueltiges UTF-8 ({e})") from e

        return HeaderMeta(
            version=self.version, scrambled=scrambled, preset=preset,
            alphabet_chars=alphabet_chars, text_encoding=text_encoding, document_id=document_id,
            page_count=page_count, page_number=page_number, page_checksum=page_checksum,
            k=k, r=r, interleave=interleave, total_file_size=total_file_size, sha256=sha256_digest,
            ln_width=ln_width, filename=filename,
        )

    # --- Hooks (versionsabhaengige Header-Felder) ---------------------------

    def _pack_flags(self, out: bytearray, scrambled: bool):
        raise NotImplementedError

    def _unpack_flags(self, rd: ByteReader) -> bool:
        """Rueckgabe: scrambled."""
        raise NotImplementedError

    def _pack_alphabet(self, out: bytearray, payload_alphabet: Alphabet, preset: str | None):
        raise NotImplementedError

    def _unpack_alphabet(self, rd: ByteReader) -> tuple[str | None, str | None]:
        """Rueckgabe: (preset, alphabet_chars), siehe HeaderMeta."""
        raise NotImplementedError

    def _pack_interleave(self, out: bytearray, interleave: int):
        raise NotImplementedError

    def _unpack_interleave(self, rd: ByteReader) -> int:
        raise NotImplementedError
