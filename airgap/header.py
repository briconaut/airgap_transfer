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
    magic:            1 Byte  (Formatversion, aktuell 6; 3 bis 5 werden weiterhin
                      gelesen und mit encode.py --format-version geschrieben)
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

Versionsunterschiede (je Version eine Klasse in airgap/formats/, die nur ihre
Abweichungen gegenueber der Vorgaengerin implementiert):
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
from collections import Counter
from dataclasses import dataclass

from .alphabets import ENCODING_UTF8, ENCODING_UTF16LE, ENCODING_UTF32LE, ENCODING_UTF32BE, PRESETS, Alphabet
from .util import digits_to_int, int_to_digits

ALPHABET_MODE_DEFAULT = 0
ALPHABET_MODE_CUSTOM = 1
ALPHABET_MODE_PRESET = 2
FLAG_SCRAMBLED = 0x01
DOCUMENT_ID_WIDTH = 8  # Byte, siehe Format.pack_header
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

MAX_PAGE_COUNT = 0xFFFF  # page_count ist ein uint16-Feld


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


@dataclass(frozen=True)
class HeaderCopyStats:
    """Qualitaet der 3-fach-Kopien eines Headers (fuer decode.py --check)."""
    lines: int  # Header-Zeilen je Kopie
    min_copies: int  # kleinste Zahl lesbarer Kopien einer Header-Zeile
    missing_copies: int  # nicht lesbare Kopien ueber alle Header-Zeilen
    disagreements: int  # Symbolpositionen, an denen sich die Kopien widersprechen


def parse_header_lines(preamble_line: str, body_lines, header_alpha: Alphabet) -> bytes:
    return parse_header_lines_with_stats(preamble_line, body_lines, header_alpha)[0]


def parse_header_lines_with_stats(preamble_line: str, body_lines,
                                  header_alpha: Alphabet) -> tuple[bytes, HeaderCopyStats]:
    """Wie parse_header_lines, zusaetzlich mit Statistik ueber die Kopien.
    Rueckgabe: (header_rohbytes, HeaderCopyStats)."""
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
    disagreements = 0
    for i in range(n):
        copies = by_line_idx[i]
        if not copies:
            raise HeaderError(f"Header-Zeile {i}: keine einzige Kopie lesbar.")
        voted, n_disagree = _majority_vote(copies)
        all_symbols.extend(voted)
        disagreements += n_disagree

    counts = [min(len(by_line_idx[i]), HEADER_COPIES) for i in range(n)]
    stats = HeaderCopyStats(lines=n, min_copies=min(counts, default=HEADER_COPIES),
                            missing_copies=sum(HEADER_COPIES - c for c in counts),
                            disagreements=disagreements)
    # floor-trick, kein expected_length noetig
    return header_alpha.symbols_to_bytes(all_symbols), stats


def _majority_vote(copies):
    """copies: Liste von Symbol-Listen (i.d.R. bis zu 3, ggf. unterschiedlich
    lang bei Kopien mit Leseartefakten). Mehrheitsentscheid pro Position ueber
    die Kopien, die an dieser Position ueberhaupt ein Symbol haben (bei
    Gleichstand gewinnt die zuerst gelesene Kopie).
    Rueckgabe: (symbole, anzahl_positionen_mit_widerspruechlichen_kopien)."""
    max_len = max(len(c) for c in copies)
    voted, disagreements = [], 0
    for pos in range(max_len):
        counter = Counter(c[pos] for c in copies if pos < len(c))
        voted.append(counter.most_common(1)[0][0])
        disagreements += len(counter) > 1
    return voted, disagreements
