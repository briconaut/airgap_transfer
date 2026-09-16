"""
Header-Format (siehe Design-Notizen der Grilling-Session, Runden 6-15):

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
    magic:            1 Byte  (Formatversion, aktuell 3)
    alphabet_mode:    1 Byte  (0 = Standard-Preset, 1 = Custom-Alphabet)
    [nur falls custom] alphabet_len: 2 Byte + alphabet_chars: alphabet_len Byte
    text_encoding:    1 Byte  (0 = UTF-8, 1 = UTF-16LE - Encoding der
                      AUSGABEDATEI selbst, siehe utf16le128/utf16le256 in
                      alphabets.py. decode.py ermittelt dies bereits vorab
                      per Byte-Sniffing der Rohdaten - dieses Feld dient nur
                      als zusaetzlicher Cross-Check, siehe decode.py)
    document_id:      8 Byte  (zufaellig, von encode.py erzeugt - identisch auf
                      allen Seiten EINES Encode-Laufs, siehe Seitenteilung unten)
    page_count:       2 Byte (uint16, big-endian) - Gesamtzahl Seiten
    page_number:      2 Byte (uint16, big-endian) - Nummer dieser Seite (0-basiert)
    page_checksum:    4 Byte (uint32, big-endian) - CRC32 ueber den Byte-Bereich
                      der Originaldatei, der auf dieser Seite steckt
    block_k:          2 Byte (uint16, big-endian)
    block_r:          2 Byte (uint16, big-endian)
    total_file_size:  8 Byte (uint64, big-endian)
    sha256:           32 Byte
    ln_width:         1 Byte  (Symbole fuer das Zeilennummernfeld der Nutzdatenzeilen)
    filename_len:     1 Byte
    filename:         filename_len Byte (UTF-8)

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
from alphabets import Alphabet, HEADER_ALPHABET

FORMAT_VERSION = 3
DOCUMENT_ID_WIDTH = 8  # Byte, siehe pack_header
TEXT_ENCODING_UTF8 = 0
TEXT_ENCODING_UTF16LE = 1
PREAMBLE_N_WIDTH = 4       # Symbole fuer die Header-Zeilenanzahl N
PREAMBLE_CHECK_WIDTH = 1   # Symbole fuer die Pruefsumme
HEADER_COPIES = 3
HEADER_COPY_IDX_WIDTH = 1  # 1..3, passt in 1 Symbol (Alphabetgroesse >= 3)
HEADER_LINE_IDX_WIDTH = 2  # bis zu 64^2=4096 Header-Zeilen (immer genug)


class HeaderError(ValueError):
    """Header nicht lesbar/nicht rekonstruierbar (harter Abbruch, siehe Design Q4/Runde15)."""


# ---------------------------------------------------------------------------
# Rohbyte-Packung
# ---------------------------------------------------------------------------

def pack_header(payload_alphabet: Alphabet, is_custom_alphabet: bool, k: int, r: int,
                 total_file_size: int, sha256_digest: bytes, ln_width: int,
                 filename: str, document_id: bytes, page_count: int, page_number: int,
                 page_checksum: int, text_encoding: int) -> bytes:
    out = bytearray()
    out.append(FORMAT_VERSION)
    out.append(1 if is_custom_alphabet else 0)
    if is_custom_alphabet:
        # utf-8 statt ascii: ein Custom-Alphabet darf bewusst ueber 7-Bit-ASCII
        # hinausgehen (siehe alphabets.py). Laengenfeld ist die BYTE-Laenge,
        # nicht die Zeichenanzahl - deshalb 2 Byte statt 1 (koennte bei vielen
        # Mehrbyte-Zeichen sonst ueberlaufen).
        chars = payload_alphabet.chars.encode("utf-8")
        out.extend(struct.pack(">H", len(chars)))
        out.extend(chars)
    if text_encoding not in (TEXT_ENCODING_UTF8, TEXT_ENCODING_UTF16LE):
        raise ValueError(f"text_encoding muss {TEXT_ENCODING_UTF8} oder {TEXT_ENCODING_UTF16LE} sein")
    out.append(text_encoding)
    if len(document_id) != DOCUMENT_ID_WIDTH:
        raise ValueError(f"document_id muss {DOCUMENT_ID_WIDTH} Byte lang sein")
    out.extend(document_id)
    out.extend(struct.pack(">H", page_count))
    out.extend(struct.pack(">H", page_number))
    out.extend(struct.pack(">I", page_checksum & 0xFFFFFFFF))
    out.extend(struct.pack(">H", k))
    out.extend(struct.pack(">H", r))
    out.extend(struct.pack(">Q", total_file_size))
    if len(sha256_digest) != 32:
        raise ValueError("sha256_digest muss 32 Byte lang sein")
    out.extend(sha256_digest)
    out.append(ln_width)
    name_bytes = filename.encode("utf-8")[:255]
    out.append(len(name_bytes))
    out.extend(name_bytes)
    return bytes(out)


def unpack_header(data: bytes) -> dict:
    pos = 0

    def take(n):
        nonlocal pos
        chunk = data[pos:pos + n]
        if len(chunk) != n:
            raise HeaderError("Header-Rohbytes zu kurz/beschaedigt")
        pos += n
        return chunk

    version = take(1)[0]
    if version != FORMAT_VERSION:
        raise HeaderError(
            f"Formatversion {version} wird von diesem Decoder nicht unterstuetzt "
            f"(bekannt: {FORMAT_VERSION}) - harter Abbruch statt Best-Effort-Versuch."
        )
    is_custom = take(1)[0] == 1
    alphabet_chars = None
    if is_custom:
        alen = struct.unpack(">H", take(2))[0]
        alphabet_chars = take(alen).decode("utf-8")
    text_encoding = take(1)[0]
    document_id = take(DOCUMENT_ID_WIDTH)
    page_count = struct.unpack(">H", take(2))[0]
    page_number = struct.unpack(">H", take(2))[0]
    page_checksum = struct.unpack(">I", take(4))[0]
    k = struct.unpack(">H", take(2))[0]
    r = struct.unpack(">H", take(2))[0]
    total_file_size = struct.unpack(">Q", take(8))[0]
    sha256_digest = take(32)
    ln_width = take(1)[0]
    name_len = take(1)[0]
    filename = take(name_len).decode("utf-8") if name_len else ""

    return dict(
        version=version, is_custom_alphabet=is_custom, alphabet_chars=alphabet_chars,
        text_encoding=text_encoding,
        document_id=document_id, page_count=page_count, page_number=page_number,
        page_checksum=page_checksum,
        k=k, r=r, total_file_size=total_file_size, sha256=sha256_digest,
        ln_width=ln_width, filename=filename,
    )


# ---------------------------------------------------------------------------
# Praeambel
# ---------------------------------------------------------------------------

def build_preamble(num_header_lines: int, header_alpha: Alphabet) -> str:
    base = header_alpha.size
    if not (0 <= num_header_lines < base ** PREAMBLE_N_WIDTH):
        raise ValueError("Header zu gross fuer die Praeambel-Feldbreite")
    digits = []
    n = num_header_lines
    for _ in range(PREAMBLE_N_WIDTH):
        digits.append(n % base)
        n //= base
    digits.reverse()
    check = sum(digits) % base
    return header_alpha.symbols_to_text(digits + [check])


def parse_preamble(line: str, header_alpha: Alphabet):
    """Rueckgabe: num_header_lines. Wirft HeaderError bei Pruefsummenfehler."""
    expected_len = PREAMBLE_N_WIDTH + PREAMBLE_CHECK_WIDTH
    if len(line) != expected_len:
        raise HeaderError(
            f"Praeambel hat {len(line)} statt {expected_len} Zeichen - Datei nicht lesbar "
            f"(Praeambel ist nicht redundant abgesichert, akzeptiertes Restrisiko)."
        )
    symbols = header_alpha.text_to_symbols(line)
    digits, check = symbols[:PREAMBLE_N_WIDTH], symbols[PREAMBLE_N_WIDTH]
    base = header_alpha.size
    if sum(digits) % base != check:
        raise HeaderError("Praeambel-Pruefsumme stimmt nicht - Datei nicht lesbar.")
    n = 0
    for d in digits:
        n = n * base + d
    return n


def find_preamble_positions(lines, header_alpha: Alphabet):
    """Findet alle Positionen in `lines`, an denen eine gueltige Praeambel
    steht (fuer Mehrseiten-Decode: jede Seite hat ihre eigene Praeambel,
    irgendwo im - moeglicherweise aus mehreren Quellen zusammengefuegten
    und/oder gemischt sortierten - Zeilenstrom). Eine Praeambel ist genau
    PREAMBLE_N_WIDTH+PREAMBLE_CHECK_WIDTH (5) Zeichen lang und hat eine
    gueltige Pruefsumme - beides zusammen macht eine zufaellige Kollision mit
    einer (viel laengeren) Header-/Nutzdatenzeile praktisch ausgeschlossen.
    Rueckgabe: sortierte Liste von Indizes in `lines`."""
    expected_len = PREAMBLE_N_WIDTH + PREAMBLE_CHECK_WIDTH
    positions = []
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if len(s) != expected_len:
            continue
        try:
            parse_preamble(s, header_alpha)
        except ValueError:
            continue
        positions.append(idx)
    return positions


# ---------------------------------------------------------------------------
# Header-Zeilen: aufteilen (Encoder) / zusammensetzen (Decoder)
# ---------------------------------------------------------------------------

def _content_width(line_width: int) -> int:
    w = line_width - HEADER_COPY_IDX_WIDTH - HEADER_LINE_IDX_WIDTH
    if w < 1:
        raise ValueError(f"--width {line_width} ist zu klein fuer Header-Zeilen")
    return w


def build_header_lines(header_bytes: bytes, line_width: int, header_alpha: Alphabet):
    """Rueckgabe: (preamble_text, [header_body_text_zeilen...]) - bereits mit
    Kopie-/Zeilenindex-Praefix, 3x wiederholt, in Reihenfolge Kopie1..Kopie3."""
    symbols = header_alpha.bytes_to_symbols(header_bytes)
    cw = _content_width(line_width)
    chunks = [symbols[i:i + cw] for i in range(0, len(symbols), cw)] or [[]]
    n = len(chunks)

    preamble = build_preamble(n, header_alpha)

    lines = []
    for copy_idx in range(1, HEADER_COPIES + 1):
        for line_idx, chunk in enumerate(chunks):
            prefix = [copy_idx] + _int_to_digits(line_idx, HEADER_LINE_IDX_WIDTH, header_alpha.size)
            lines.append(header_alpha.symbols_to_text(prefix + chunk))
    return preamble, lines


def parse_header_lines(preamble_line: str, body_lines, line_width: int, header_alpha: Alphabet) -> bytes:
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
        if len(symbols) < HEADER_COPY_IDX_WIDTH + HEADER_LINE_IDX_WIDTH:
            continue
        copy_idx = symbols[0]
        line_idx = _digits_to_int(symbols[1:1 + HEADER_LINE_IDX_WIDTH], header_alpha.size)
        content = symbols[1 + HEADER_LINE_IDX_WIDTH:]
        if line_idx in by_line_idx:
            by_line_idx[line_idx].append(content)

    all_symbols = []
    for i in range(n):
        copies = by_line_idx[i]
        if not copies:
            raise HeaderError(f"Header-Zeile {i}: keine einzige Kopie lesbar - Abbruch.")
        all_symbols.extend(_majority_vote(copies))

    header_bytes = header_alpha.symbols_to_bytes(all_symbols)  # floor-trick, kein expected_length noetig
    return header_bytes


def _majority_vote(copies):
    """copies: Liste von Symbol-Listen (i.d.R. bis zu 3, ggf. unterschiedlich
    lang bei Kopien mit Leseartefakten). Mehrheitsentscheid pro Position ueber
    die Kopien, die an dieser Position ueberhaupt ein Symbol haben."""
    max_len = max(len(c) for c in copies)
    out = []
    for pos in range(max_len):
        votes = {}
        for c in copies:
            if pos < len(c):
                votes[c[pos]] = votes.get(c[pos], 0) + 1
        if not votes:
            raise HeaderError("Header-Position ohne jede lesbare Kopie - Abbruch.")
        best = max(votes.items(), key=lambda kv: kv[1])[0]
        out.append(best)
    return out


def _int_to_digits(value: int, width: int, base: int):
    digits = []
    v = value
    for _ in range(width):
        digits.append(v % base)
        v //= base
    digits.reverse()
    if v != 0:
        raise ValueError(f"Wert {value} passt nicht in {width} Symbole (Basis {base})")
    return digits


def _digits_to_int(digits, base: int) -> int:
    v = 0
    for d in digits:
        v = v * base + d
    return v
