"""
Header-Format:

Praeambel (genau 1x, KEINE Wiederholung, akzeptiertes Restrisiko):
    [N: 4 Header-Alphabet-Symbole][Check: 1 Symbol]
    N = Anzahl Header-Koerper-Zeilen PRO KOPIE.
    Check = Summe der 4 N-Symbole modulo Alphabetgroesse (reine Erkennung,
    keine Korrektur).

Header-Koerper (3x wiederholt, jede Zeile mit Praefix):
    [Kopie-Index: 1 Symbol (1..3)][Zeilen-Index: 2 Symbole][Inhalt: Rest]
    Decoder gruppiert per (Zeilen-Index), macht Mehrheitsentscheid ueber die
    (bis zu 3) Kopien jeder Position, setzt den Header wieder zusammen.

Header-Rohbytes (das, was bit-geslict wird):
    magic:            1 Byte  (Formatversion, aktuell 6; 3 bis 5 werden weiterhin gelesen)
    [ab Version 4] flags: 1 Byte (Bit 0 = Nutzdaten verwuerfelt, siehe scramble.py;
                      Version 3 hat kein flags-Feld und gilt als unverwuerfelt)
    alphabet_mode:    1 Byte  (0 = Preset 'default', 1 = Custom-Alphabet,
                      2 = anderes Preset [ab Version 6])
    [mode 1]          alphabet_len: 2 Byte + alphabet_chars: alphabet_len Byte (UTF-8)
    [mode 2]          preset_id: 1 Byte (siehe alphabets.PRESET_IDS)
    text_encoding:    1 Byte  (0 = UTF-8, 1 = UTF-16LE, 2 = UTF-32LE,
                      3 = UTF-32BE - Encoding der AUSGABEDATEI selbst, siehe
                      utf16le128/utf16le256/utf32le128/utf32le256/utf32le512/
                      utf32be512 in alphabets.py. decode.py ermittelt dies
                      bereits vorab per Byte-Sniffing der Rohdaten - dieses
                      Feld dient nur als zusaetzlicher Cross-Check, siehe
                      decode.py)
    document_id:      8 Byte  (zufaellig, von encode.py erzeugt - identisch auf
                      allen Seiten EINES Encode-Laufs, siehe Seitenteilung unten)
    page_count:       2 Byte (uint16, big-endian) - Gesamtzahl Seiten
    page_number:      2 Byte (uint16, big-endian) - Nummer dieser Seite (0-basiert)
    page_checksum:    4 Byte (uint32, big-endian) - CRC32 ueber den Byte-Bereich
                      der Originaldatei, der auf dieser Seite steckt
    block_k:          2 Byte (uint16, big-endian) - Datensymbole JE CODEWORT
    block_r:          2 Byte (uint16, big-endian) - Paritaetssymbole JE CODEWORT
    [ab Version 6] interleave: 1 Byte - RS-Codewoerter je Nutzdatenzeile
                      (verschraenkt, siehe framing.LineCode; bis Version 5 immer 1)
    total_file_size:  8 Byte (uint64, big-endian)
    sha256:           32 Byte
    ln_width:         1 Byte  (Symbole fuer das Zeilennummernfeld der Nutzdatenzeilen)
    filename_len:     1 Byte
    filename:         filename_len Byte (UTF-8)

Versionsunterschiede beim Lesen:
    3 -> 4: flags-Byte (Verwuerfelung) eingefuehrt.
    4 -> 5: Das CRC32 jeder Nutzdatenzeile deckt bei Alphabeten mit mehr als
            8 Bit/Symbol (GF512) das VOLLSTAENDIGE Symbol ab (2 Byte/Symbol,
            siehe framing.symbols_crc32); bis Version 4 wurde dort nur das
            untere Byte jedes Symbols erfasst. Fuer Alphabete <= 256 Zeichen
            sind Version 4 und 5 bit-identisch.
    5 -> 6: mehrere verschraenkte RS-Codewoerter je Zeile (interleave-Feld),
            dadurch ist die Zeilenbreite nicht mehr durch die Alphabetgroesse
            begrenzt; Presets werden per Kennung statt mit allen Zeichen
            gespeichert (alphabet_mode 2) - beim 512er-Preset schrumpft der
            Header so von ~30 auf ~1 Zeile je Kopie.

Seitenteilung (encode.py --lines):
    Jede Seite traegt eine VOLLSTAENDIGE Kopie dieses Headers (inkl. Praeambel
    und 3-facher Wiederholung, siehe unten) - nur page_number/page_checksum
    unterscheiden sich zwischen den Seiten einer Datei, alle anderen Felder
    sind identisch. Zeilennummern in den Nutzdatenzeilen bleiben GLOBAL ueber
    die gesamte Datei (unveraendert durch die Seitenteilung) - eine Seite ist
    rein eine Wiederholungs-/Framing-Einheit, keine eigene Adressierung.
    Ohne --lines: page_count=1, page_number=0, page_checksum ueber die
    gesamte Datei (degenerierter Einzelseiten-Fall, gleicher Code-Pfad).
"""
import struct
from collections import Counter
from dataclasses import dataclass

from .alphabets import (
    ENCODING_UTF8, ENCODING_UTF16LE, ENCODING_UTF32LE, ENCODING_UTF32BE, PRESET_IDS, PRESETS,
    PRESETS_BY_ID, Alphabet,
)
from .util import digits_to_int, int_to_digits

FORMAT_VERSION = 6
SUPPORTED_FORMAT_VERSIONS = (3, 4, 5, 6)  # aeltere Versionen nur lesen, siehe Modul-Docstring
FIRST_VERSION_WITH_FLAGS = 4
FIRST_VERSION_WITH_WIDE_CRC = 5
FIRST_VERSION_WITH_INTERLEAVE = 6
ALPHABET_MODE_DEFAULT = 0
ALPHABET_MODE_CUSTOM = 1
ALPHABET_MODE_PRESET = 2
FLAG_SCRAMBLED = 0x01
DOCUMENT_ID_WIDTH = 8  # Byte, siehe pack_header
SHA256_WIDTH = 32
TEXT_ENCODING_UTF8 = 0
TEXT_ENCODING_UTF16LE = 1
TEXT_ENCODING_UTF32LE = 2
TEXT_ENCODING_UTF32BE = 3

# text_encoding-Feld <-> Python-Codec der Ausgabedatei <-> Anzeigename.
# Einzige Stelle fuer diese Zuordnung (encode.py, decode.py und reframe.py
# benutzen sie alle).
TEXT_ENCODING_TO_CODEC = {
    TEXT_ENCODING_UTF8: ENCODING_UTF8,
    TEXT_ENCODING_UTF16LE: ENCODING_UTF16LE,
    TEXT_ENCODING_UTF32LE: ENCODING_UTF32LE,
    TEXT_ENCODING_UTF32BE: ENCODING_UTF32BE,
}
CODEC_TO_TEXT_ENCODING = {codec: enc for enc, codec in TEXT_ENCODING_TO_CODEC.items()}
TEXT_ENCODING_NAMES = {
    TEXT_ENCODING_UTF8: "UTF-8",
    TEXT_ENCODING_UTF16LE: "UTF-16LE",
    TEXT_ENCODING_UTF32LE: "UTF-32LE",
    TEXT_ENCODING_UTF32BE: "UTF-32BE",
}

PREAMBLE_N_WIDTH = 4       # Symbole fuer die Header-Zeilenanzahl N
PREAMBLE_CHECK_WIDTH = 1   # Symbole fuer die Pruefsumme
PREAMBLE_LEN = PREAMBLE_N_WIDTH + PREAMBLE_CHECK_WIDTH
HEADER_COPIES = 3
HEADER_COPY_IDX_WIDTH = 1  # 1..3, passt in 1 Symbol (Alphabetgroesse >= 3)
HEADER_LINE_IDX_WIDTH = 2  # bis zu 64^2=4096 Header-Zeilen (immer genug)
HEADER_PREFIX_WIDTH = HEADER_COPY_IDX_WIDTH + HEADER_LINE_IDX_WIDTH

_UINT8_MAX = 0xFF
_UINT16_MAX = 0xFFFF
_UINT64_MAX = 0xFFFF_FFFF_FFFF_FFFF
MAX_PAGE_COUNT = _UINT16_MAX  # page_count ist ein uint16-Feld


class HeaderError(ValueError):
    """Header nicht lesbar/nicht rekonstruierbar (diese Seite wird uebersprungen)."""


@dataclass(frozen=True)
class HeaderMeta:
    """Inhalt eines entpackten Seiten-Headers (siehe Modul-Docstring)."""
    version: int
    scrambled: bool
    preset: str | None  # Preset-Name; None bei Custom-Alphabet (oder Preset vor Version 6)
    alphabet_chars: str | None  # nur bei Custom-Alphabet (bis Version 5 auch fuer Presets ausser 'default')
    text_encoding: int
    document_id: bytes
    page_count: int
    page_number: int
    page_checksum: int
    k: int  # Datensymbole je Codewort
    r: int  # Paritaetssymbole je Codewort
    interleave: int  # Codewoerter je Zeile
    total_file_size: int
    sha256: bytes
    ln_width: int
    filename: str

    @property
    def payload_chars(self) -> str:
        """Zeichenfolge des Nutzdaten-Alphabets."""
        return self.alphabet_chars if self.preset is None else PRESETS[self.preset].chars

    @property
    def alphabet_label(self) -> str:
        """Beschreibung des Nutzdaten-Alphabets fuer Ausgaben."""
        if self.preset is not None:
            return f"Preset '{self.preset}'"
        return f"eigenes Alphabet ({len(self.alphabet_chars)} Zeichen): {self.alphabet_chars}"


# ---------------------------------------------------------------------------
# Rohbyte-Packung
# ---------------------------------------------------------------------------

def _check_range(name: str, value: int, maximum: int):
    if not 0 <= value <= maximum:
        raise ValueError(f"{name}={value} liegt ausserhalb des Header-Feldbereichs 0..{maximum}")


def pack_header(payload_alphabet: Alphabet, preset: str | None, k: int, r: int, interleave: int,
                total_file_size: int, sha256_digest: bytes, ln_width: int,
                filename: str, document_id: bytes, page_count: int, page_number: int,
                page_checksum: int, text_encoding: int, scrambled: bool = False) -> bytes:
    """preset: Preset-Name oder None fuer ein Custom-Alphabet (dann werden die
    Zeichen von payload_alphabet gespeichert)."""
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
    _check_range("interleave", interleave, _UINT8_MAX)
    _check_range("total_file_size", total_file_size, _UINT64_MAX)
    _check_range("ln_width", ln_width, _UINT8_MAX)

    out = bytearray()
    out.append(FORMAT_VERSION)
    out.append(FLAG_SCRAMBLED if scrambled else 0)
    if preset == "default":
        out.append(ALPHABET_MODE_DEFAULT)
    elif preset is not None:
        out.append(ALPHABET_MODE_PRESET)
        out.append(PRESET_IDS[preset])
    else:
        # utf-8 statt ascii: ein Custom-Alphabet darf bewusst ueber 7-Bit-ASCII
        # hinausgehen (siehe alphabets.py). Laengenfeld ist die BYTE-Laenge,
        # nicht die Zeichenanzahl - deshalb 2 Byte statt 1 (koennte bei vielen
        # Mehrbyte-Zeichen sonst ueberlaufen).
        out.append(ALPHABET_MODE_CUSTOM)
        chars = payload_alphabet.chars.encode("utf-8")
        out.extend(struct.pack(">H", len(chars)))
        out.extend(chars)
    out.append(text_encoding)
    out.extend(document_id)
    out.extend(struct.pack(">HHIHHBQ", page_count, page_number, page_checksum & 0xFFFFFFFF,
                           k, r, interleave, total_file_size))
    out.extend(sha256_digest)
    out.append(ln_width)
    name_bytes = _truncate_utf8(filename, _UINT8_MAX)
    out.append(len(name_bytes))
    out.extend(name_bytes)
    return bytes(out)


def _truncate_utf8(text: str, max_bytes: int) -> bytes:
    """UTF-8-Kodierung von `text`, auf hoechstens `max_bytes` gekuerzt, ohne
    ein Mehrbyte-Zeichen zu zerschneiden (sonst waere der Name beim Lesen
    kein gueltiges UTF-8 mehr)."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")


def unpack_header(data: bytes) -> HeaderMeta:
    pos = 0

    def take(n):
        nonlocal pos
        chunk = data[pos:pos + n]
        if len(chunk) != n:
            raise HeaderError("Header-Rohbytes zu kurz/beschaedigt")
        pos += n
        return chunk

    version = take(1)[0]
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise HeaderError(
            f"Formatversion {version} wird von diesem Decoder nicht unterstuetzt "
            f"(bekannt: {SUPPORTED_FORMAT_VERSIONS})."
        )
    flags = take(1)[0] if version >= FIRST_VERSION_WITH_FLAGS else 0
    alphabet_mode = take(1)[0]
    preset = alphabet_chars = None
    try:
        if alphabet_mode == ALPHABET_MODE_DEFAULT:
            preset = "default"
        elif alphabet_mode == ALPHABET_MODE_CUSTOM:
            (alen,) = struct.unpack(">H", take(2))
            alphabet_chars = take(alen).decode("utf-8")
        elif alphabet_mode == ALPHABET_MODE_PRESET and version >= FIRST_VERSION_WITH_INTERLEAVE:
            preset_id = take(1)[0]
            if preset_id not in PRESETS_BY_ID:
                raise HeaderError(f"Unbekannte Preset-Kennung {preset_id} (neuere Programmversion?)")
            preset = PRESETS_BY_ID[preset_id]
        else:
            raise HeaderError(f"Unbekannter Alphabet-Modus {alphabet_mode}")
        text_encoding = take(1)[0]
        document_id = take(DOCUMENT_ID_WIDTH)
        page_count, page_number, page_checksum, k, r = struct.unpack(">HHIHH", take(12))
        interleave = take(1)[0] if version >= FIRST_VERSION_WITH_INTERLEAVE else 1
        (total_file_size,) = struct.unpack(">Q", take(8))
        sha256_digest = take(SHA256_WIDTH)
        ln_width = take(1)[0]
        name_len = take(1)[0]
        filename = take(name_len).decode("utf-8")
    except UnicodeDecodeError as e:
        raise HeaderError(f"Header enthaelt ungueltiges UTF-8 ({e})") from e

    return HeaderMeta(
        version=version, scrambled=bool(flags & FLAG_SCRAMBLED), preset=preset,
        alphabet_chars=alphabet_chars, text_encoding=text_encoding, document_id=document_id,
        page_count=page_count, page_number=page_number, page_checksum=page_checksum,
        k=k, r=r, interleave=interleave, total_file_size=total_file_size, sha256=sha256_digest,
        ln_width=ln_width, filename=filename,
    )


# ---------------------------------------------------------------------------
# Praeambel
# ---------------------------------------------------------------------------

def build_preamble(num_header_lines: int, header_alpha: Alphabet) -> str:
    try:
        digits = int_to_digits(num_header_lines, PREAMBLE_N_WIDTH, header_alpha.size)
    except ValueError as e:
        raise ValueError("Header zu gross fuer die Praeambel-Feldbreite") from e
    check = sum(digits) % header_alpha.size
    return header_alpha.symbols_to_text(digits + [check])


def parse_preamble(line: str, header_alpha: Alphabet) -> int:
    """Rueckgabe: num_header_lines. Wirft HeaderError bei Laengen-,
    Zeichen- oder Pruefsummenfehler."""
    if len(line) != PREAMBLE_LEN:
        raise HeaderError(
            f"Praeambel hat {len(line)} statt {PREAMBLE_LEN} Zeichen - Seite nicht lesbar "
            f"(Praeambel ist nicht redundant abgesichert, akzeptiertes Restrisiko)."
        )
    try:
        symbols = header_alpha.text_to_symbols(line)
    except ValueError as e:
        raise HeaderError(f"Praeambel nicht lesbar ({e})") from e
    digits, check = symbols[:PREAMBLE_N_WIDTH], symbols[PREAMBLE_N_WIDTH]
    if sum(digits) % header_alpha.size != check:
        raise HeaderError("Praeambel-Pruefsumme stimmt nicht - Seite nicht lesbar.")
    return digits_to_int(digits, header_alpha.size)


def find_preamble_positions(lines, header_alpha: Alphabet) -> list[int]:
    """Findet alle Positionen in `lines`, an denen eine gueltige Praeambel
    steht (fuer Mehrseiten-Decode: jede Seite hat ihre eigene Praeambel,
    irgendwo im - moeglicherweise aus mehreren Quellen zusammengefuegten
    und/oder gemischt sortierten - Zeilenstrom). Eine Praeambel ist genau
    PREAMBLE_LEN (5) Zeichen lang und hat eine gueltige Pruefsumme - beides
    zusammen macht eine zufaellige Kollision mit einer (viel laengeren)
    Header-/Nutzdatenzeile praktisch ausgeschlossen.
    Rueckgabe: sortierte Liste von Indizes in `lines`."""
    positions = []
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if len(s) != PREAMBLE_LEN:
            continue
        try:
            parse_preamble(s, header_alpha)
        except HeaderError:
            continue
        positions.append(idx)
    return positions


# ---------------------------------------------------------------------------
# Header-Zeilen: aufteilen (Encoder) / zusammensetzen (Decoder)
# ---------------------------------------------------------------------------

def _content_width(line_width: int) -> int:
    w = line_width - HEADER_PREFIX_WIDTH
    if w < 1:
        raise ValueError(f"--width {line_width} ist zu klein fuer Header-Zeilen")
    return w


def build_header_lines(header_bytes: bytes, line_width: int, header_alpha: Alphabet):
    """Rueckgabe: (preamble_text, [header_body_text_zeilen...]) - bereits mit
    Kopie-/Zeilenindex-Praefix, 3x wiederholt, in Reihenfolge Kopie1..Kopie3."""
    symbols = header_alpha.bytes_to_symbols(header_bytes)
    cw = _content_width(line_width)
    chunks = [symbols[i:i + cw] for i in range(0, len(symbols), cw)] or [[]]

    preamble = build_preamble(len(chunks), header_alpha)

    lines = []
    for copy_idx in range(1, HEADER_COPIES + 1):
        for line_idx, chunk in enumerate(chunks):
            prefix = [copy_idx] + int_to_digits(line_idx, HEADER_LINE_IDX_WIDTH, header_alpha.size)
            lines.append(header_alpha.symbols_to_text(prefix + chunk))
    return preamble, lines


def parse_header_lines(preamble_line: str, body_lines, header_alpha: Alphabet) -> bytes:
    n = parse_preamble(preamble_line, header_alpha)
    expected_total = n * HEADER_COPIES
    # Bewusst KEIN harter Abbruch bei zu wenigen Zeilen: jede Header-Zeile
    # traegt ihren eigenen Kopie-/Zeilenindex, daher wird unten pro
    # Zeilen-Index ausgewertet, wie viele der (bis zu 3) Kopien tatsaechlich
    # da sind - eine komplett verlorene Zeile faellt hoechstens auf 2 Kopien
    # zurueck statt sofort abzubrechen (bekannte Einschraenkung: siehe README
    # zur Grenze zwischen Header- und Nutzdatenbereich).

    # pro Zeilen-Index: Liste der (bis zu 3) gefundenen Kopien sammeln
    by_line_idx = {i: [] for i in range(n)}
    for raw in body_lines[:expected_total]:
        try:
            symbols = header_alpha.text_to_symbols(raw.strip())
        except ValueError:
            continue  # unlesbare Zeile: einfach ignorieren, zaehlt als fehlende Kopie
        if len(symbols) < HEADER_PREFIX_WIDTH:
            continue
        copy_idx = symbols[0]
        if not 1 <= copy_idx <= HEADER_COPIES:
            continue  # Kopie-Index verfaelscht (oder gar keine Header-Zeile): nicht mitzaehlen
        line_idx = digits_to_int(symbols[HEADER_COPY_IDX_WIDTH:HEADER_PREFIX_WIDTH], header_alpha.size)
        if line_idx in by_line_idx:
            by_line_idx[line_idx].append(symbols[HEADER_PREFIX_WIDTH:])

    all_symbols = []
    for i in range(n):
        copies = by_line_idx[i]
        if not copies:
            raise HeaderError(f"Header-Zeile {i}: keine einzige Kopie lesbar.")
        all_symbols.extend(_majority_vote(copies))

    return header_alpha.symbols_to_bytes(all_symbols)  # floor-trick, kein expected_length noetig


def _majority_vote(copies):
    """copies: Liste von Symbol-Listen (i.d.R. bis zu 3, ggf. unterschiedlich
    lang bei Kopien mit Leseartefakten). Mehrheitsentscheid pro Position ueber
    die Kopien, die an dieser Position ueberhaupt ein Symbol haben (bei
    Gleichstand gewinnt die zuerst gelesene Kopie)."""
    max_len = max(len(c) for c in copies)
    return [
        Counter(c[pos] for c in copies if pos < len(c)).most_common(1)[0][0]
        for pos in range(max_len)
    ]
