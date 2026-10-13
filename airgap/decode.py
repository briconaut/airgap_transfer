"""
Decoder: OCR-Text/Seiten -> Datei(en) + Fehlerbericht.

Beispiel:
    python3 bin/decode.py ocr_output.txt -o wiederhergestellt
    python3 bin/decode.py seite1.txt seite2.txt seite3.txt -o wiederhergestellt
    python3 bin/decode.py "seite_*.txt" -o wiederhergestellt
    -> erzeugt entweder
         wiederhergestellt              (wenn alles fehlerfrei rekonstruiert wurde)
       oder bei Fehlern/Luecken:
         wiederhergestellt.part1        (sauberer Anfangsteil)
         wiederhergestellt.part2        (Rest, inkl. nicht rekonstruierbarer
                                          Stellen als Nullbytes)
       sowie in beiden Faellen
         wiederhergestellt.errors.txt   (Fehlerbericht, Format via --error-report-format)

Exitcodes:
    0  vollstaendig rekonstruiert, SHA-256 stimmt
    1  Abbruch (keine lesbare Seite, unlesbare Eingabe, gemischte Dokumente, ...)
    2  ungueltige Kommandozeile (argparse)
    3  unvollstaendig rekonstruiert (part1/part2 geschrieben)
    4  vollstaendig rekonstruiert, aber SHA-256 stimmt NICHT

Eingabe kann aus einer einzigen Datei/stdin (ggf. mit mehreren Seiten
hintereinander, in beliebiger Reihenfolge), mehreren einzelnen Dateien oder
einem Glob-Pattern bestehen - decode.py sucht ueberall im kombinierten
Zeilenstrom nach Seiten-Praeambeln und setzt die Seiten anhand ihrer
Zeilennummern zusammen, unabhaengig von der Reihenfolge der Eingabe.

UTF-8 vs. UTF-16LE vs. UTF-32LE vs. UTF-32BE (siehe --preset utf16le128/
utf16le256/utf32le128/utf32le256/utf32le512/utf32be512 in encode.py) wird
PRO QUELLE automatisch anhand der Rohbytes erkannt, ohne die Datei neu zu
oeffnen: das Header-Alphabet ist immer reines 7-Bit-ASCII und 0x00 ist in
jedem Alphabet verboten, also kann UTF-8 nie ein rohes 0x00-Byte enthalten,
waehrend ASCII als UTF-16LE immer "Byte, 0x00"-Paare und als UTF-32LE/
UTF-32BE immer drei 0x00-Bytes je Zeichen erzeugt - eindeutig erkennbar an
den ersten paar Bytes, auch ohne BOM (wichtig, wenn eine Seite als eigene
Datei ohne den fuehrenden BOM der Gesamtdatei vorliegt).

Der Header EINER Seite ist nicht ueber mehrere Seiten redundant abgesichert
(siehe README.md) - ist er nicht lesbar (auch: unbekannte Formatversion),
wird NUR diese Seite uebersprungen; erst wenn KEINE einzige Seite einen
lesbaren Header liefert, bricht das Tool komplett ab (kein Best-Effort-
Rateversuch ohne jede Header-Information).
"""
import argparse
import glob
import hashlib
import json
import sys
import zlib
from collections import defaultdict
from dataclasses import dataclass, field

from .alphabets import DEFAULT_ALPHABET, HEADER_ALPHABET, Alphabet
from .framing import (
    LineResult, LineStatus, byte_offset_of_line, crc_is_wide, crc_width_for, parse_payload_line,
    reflow_joined_lines, total_lines_for, unarrange_columns,
)
from .header import (
    HEADER_COPIES, TEXT_ENCODING_NAMES, TEXT_ENCODING_TO_CODEC, HeaderError, HeaderMeta,
    find_preamble_positions, parse_header_lines, parse_preamble, unpack_header,
)
from .rs_codec import GaloisField
from .scramble import scramble_block

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_INCOMPLETE = 3
EXIT_HASH_MISMATCH = 4


class FatalError(Exception):
    """Abbruch des gesamten Laufs (Exitcode 1), Text = Meldung fuer stderr."""


# ---------------------------------------------------------------------------
# Eingabe lesen
# ---------------------------------------------------------------------------

def gather_input_paths(input_args):
    """input_args: Liste von Pfaden/Glob-Patterns. Expandiert Wildcards, bricht
    hart ab wenn ein Pattern auf keine Datei passt (Tippfehler-Schutz)."""
    paths = []
    for tok in input_args:
        if any(c in tok for c in "*?["):
            matches = sorted(glob.glob(tok))
            if not matches:
                raise FatalError(f"FEHLER: Pattern {tok!r} passt auf keine Datei.")
            paths.extend(matches)
        else:
            paths.append(tok)
    return paths


def sniff_layout(raw: bytes):
    """Erkennt anhand der Rohbytes, ob ein 8-Bit-Strom (UTF-8/Latin-1) oder
    UTF-16LE/UTF-32LE/UTF-32BE vorliegt - kein erneutes Oeffnen/Lesen noetig
    (siehe Modul-Docstring). Ein fuehrendes BOM wird erkannt, ist aber fuer
    die Erkennung selbst NICHT erforderlich: da 0x00 in jedem Alphabet
    verboten ist (siehe Alphabet.__init__ in alphabets.py) und das
    Header-Alphabet immer reines 7-Bit-ASCII ist, kann ein 8-Bit-Strom nie
    ein rohes 0x00-Byte enthalten, waehrend ASCII als UTF-16LE IMMER 'Byte,
    0x00'-Paare und als UTF-32LE/UTF-32BE IMMER drei 0x00-Bytes je Zeichen
    erzeugt - eindeutig unterscheidbar an den ersten Bytes.

    Das UTF-16LE-BOM (FF FE) ist ein reiner Byte-PRAEFIX des UTF-32LE-BOM
    (FF FE 00 00) - deshalb wird zuerst auf die laengeren/spezifischeren
    4-Byte-Muster (BOM und Byte-Sniffing) geprueft, bevor auf die kuerzeren
    2-Byte-Muster zurueckgefallen wird.
    Rueckgabe: (codec, bom_len). codec ist "utf-8" fuer jeden 8-Bit-Strom
    (entspricht text_encoding=UTF-8 im Header), ob die Nutzdaten nun
    tatsaechlich UTF-8 oder Latin-1 sind (siehe decode_payload_text)."""
    if raw[:4] == b"\xff\xfe\x00\x00":
        return "utf-32-le", 4
    if raw[:4] == b"\x00\x00\xfe\xff":
        return "utf-32-be", 4
    if raw[:2] == b"\xff\xfe":
        return "utf-16-le", 2
    if len(raw) >= 4 and raw[1] == 0 and raw[2] == 0 and raw[3] == 0:
        return "utf-32-le", 0
    if len(raw) >= 4 and raw[0] == 0 and raw[1] == 0 and raw[2] == 0:
        return "utf-32-be", 0
    if len(raw) >= 2 and raw[1] == 0:
        return "utf-16-le", 0
    return "utf-8", 0


def split_text_lines(text: str):
    # Binaeres Lesen umgeht Pythons Universal-Newlines-Uebersetzung (die ein
    # Text-mode open() automatisch macht) - von Hand nachholen, falls die
    # Quelle mit \r\n oder \r statt \n endet (z.B. unter Windows erzeugt).
    # Ein mehrspaltiges Layout (encode.py --columns) wird dabei gleich wieder
    # in eine einzige Zeilenfolge aufgeloest.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return unarrange_columns(lines)[0]


def decode_header_text(raw: bytes):
    """ASCII-Sicht einer Quelle fuer Praeambel und Header: das Header-Alphabet
    ist immer reines 7-Bit-ASCII, also wird jedes Nicht-ASCII-Zeichen durch
    U+FFFD ersetzt (macht nur Nutzdatenzeilen 'unlesbar', die hier ohnehin
    nicht interessieren). Schlaegt nie fehl - ein Nutzdaten-Zeichen, das im
    jeweiligen Text-Encoding ungueltig ist, kann so das Lesen des Headers
    nicht mehr verhindern. Die Zeilenstruktur stimmt mit decode_payload_text
    ueberein (Zeilenumbrueche sind ASCII), Zeilenindizes sind also in beiden
    Sichten identisch."""
    codec, bom_len = sniff_layout(raw)
    if codec == "utf-8":
        return raw.decode("ascii", errors="replace")
    text = raw[bom_len:].decode(codec, errors="replace")
    return "".join(c if c < "\x80" else "�" for c in text)


def decode_payload_text(raw: bytes):
    """Volle Text-Sicht einer Quelle fuer die Nutzdatenzeilen. Ein 8-Bit-Strom
    wird als UTF-8 gelesen, und - falls das scheitert - als Latin-1 (z.B.
    wenn OCR-Tool oder Editor die Seite als 'ANSI' gespeichert haben; das
    latin128-Preset liegt komplett im Latin-1-Bereich).
    Rueckgabe: (text, layout_codec, text_codec). Wirft UnicodeDecodeError
    nur bei kaputtem UTF-16/UTF-32."""
    codec, bom_len = sniff_layout(raw)
    if codec != "utf-8":
        return raw[bom_len:].decode(codec), codec, codec
    try:
        return raw.decode("utf-8"), codec, "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), codec, "latin-1"


def read_sources(input_args):
    """Liest alle Quellen (stdin, oder eine/mehrere Dateien/Patterns) als
    Rohbytes. Rueckgabe: Liste von (name, raw)."""
    if not input_args:
        return [("<stdin>", sys.stdin.buffer.read())]
    sources = []
    for path in gather_input_paths(input_args):
        try:
            with open(path, "rb") as f:
                sources.append((path, f.read()))
        except OSError as e:
            raise FatalError(f"FEHLER: {path} nicht lesbar ({e}).") from e
    return sources


def read_all_input_lines(sources):
    """Decodiert jede Quelle EINZELN (Header als ASCII, Nutzdaten im
    erkannten Text-Encoding) und haengt die resultierenden Zeilenlisten
    aneinander - sonst koennte die letzte Zeile einer Datei mit der ersten
    Zeile der naechsten verschmelzen.

    Rueckgabe: (header_lines, payload_lines, source_ranges). Beide
    Zeilenlisten sind gleich lang und indexgleich. source_ranges: Liste von
    (start_idx, end_idx_exklusiv, layout_codec) pro Quelle, fuer den
    spaeteren Cross-Check gegen das von jeder Seite im Header deklarierte
    text_encoding-Feld (siehe read_pages)."""
    header_lines = []
    payload_lines = []
    source_ranges = []
    for name, raw in sources:
        try:
            text, layout_codec, text_codec = decode_payload_text(raw)
        except UnicodeDecodeError as e:
            raise FatalError(
                f"FEHLER: {name} laesst sich nicht als {sniff_layout(raw)[0].upper()} lesen ({e})."
            ) from e
        if text_codec == "latin-1":
            print(f"Hinweis: {name} ist kein gueltiges UTF-8 - Nutzdaten werden als Latin-1 gelesen.",
                  file=sys.stderr)
        h_lines = split_text_lines(decode_header_text(raw))
        p_lines = split_text_lines(text)
        if len(h_lines) != len(p_lines):
            # Kann nur passieren, wenn ein Nicht-ASCII-Whitespace-Zeichen
            # (z.B. U+0085) die Spaltenaufteilung der beiden Sichten
            # unterschiedlich beeinflusst - dann ist die Zuordnung unsicher.
            raise FatalError(
                f"FEHLER: {name}: Zeilenstruktur von Header- und Nutzdaten-Sicht weicht ab "
                f"({len(h_lines)} vs. {len(p_lines)} Zeilen) - Eingabe vermutlich beschaedigt."
            )
        start = len(header_lines)
        header_lines.extend(h_lines)
        payload_lines.extend(p_lines)
        source_ranges.append((start, len(header_lines), layout_codec))
    return header_lines, payload_lines, source_ranges


def encoding_at(idx, source_ranges):
    for start, end, encoding in source_ranges:
        if start <= idx < end:
            return encoding
    raise IndexError(f"Zeilenindex {idx} liegt in keiner Quelle")


# ---------------------------------------------------------------------------
# Seiten-Header
# ---------------------------------------------------------------------------

def _validate_meta(meta: HeaderMeta):
    """Plausibilitaetspruefung eines entpackten Headers. Die Header-Kopien
    sind per Mehrheitsentscheid rekonstruiert - bei sehr schlechter Vorlage
    koennen trotzdem unsinnige Werte herauskommen, die spaeter zu
    Abstuerzen statt zu einer sauberen Fehlermeldung fuehren wuerden."""
    if meta.is_custom_alphabet:
        try:
            payload_alpha = Alphabet(meta.alphabet_chars)
        except ValueError as e:
            raise HeaderError(f"Alphabet im Header ungueltig ({e})") from e
        size = payload_alpha.size
    else:
        size = len(DEFAULT_ALPHABET)
    if meta.k < 1 or meta.r < 1 or meta.k + meta.r > size - 1:
        raise HeaderError(f"k={meta.k}/r={meta.r} passen nicht zu einem Alphabet mit {size} Zeichen")
    if meta.ln_width < 1:
        raise HeaderError("Zeilennummernbreite 0 im Header")
    if not 0 <= meta.page_number < meta.page_count:
        raise HeaderError(f"Seitennummer {meta.page_number} passt nicht zu {meta.page_count} Seiten")
    if meta.text_encoding not in TEXT_ENCODING_TO_CODEC:
        raise HeaderError(f"Unbekanntes text_encoding {meta.text_encoding}")


def parse_page_header(seg, header_alpha):
    """seg: Zeilen (ASCII-Sicht) ab einer Praeambel. Rueckgabe: (meta,
    n_header_body_lines). Wirft HeaderError."""
    preamble_line = seg[0].strip()
    n_header_lines = parse_preamble(preamble_line, header_alpha)
    expected_header_body = n_header_lines * HEADER_COPIES
    body_window = seg[1:1 + expected_header_body]
    meta = unpack_header(parse_header_lines(preamble_line, body_window, header_alpha))
    _validate_meta(meta)
    return meta, expected_header_body


def format_header(meta: HeaderMeta):
    if meta.is_custom_alphabet:
        alphabet = f"eigenes Alphabet ({len(meta.alphabet_chars)} Zeichen): {meta.alphabet_chars}"
    else:
        alphabet = "Standard-Alphabet"
    enc = meta.text_encoding
    rows = [
        ("Formatversion", meta.version),
        ("Verwuerfelt", "ja" if meta.scrambled else "nein"),
        ("Alphabet", alphabet),
        ("Text-Encoding", f"{TEXT_ENCODING_NAMES.get(enc, 'unbekannt')} ({enc})"),
        ("Dokument-ID", meta.document_id.hex()),
        ("Seite", f"{meta.page_number} (0-basiert) von {meta.page_count}"),
        ("Seiten-Pruefsumme", f"0x{meta.page_checksum:08x}"),
        ("Block k / r", f"{meta.k} / {meta.r}"),
        ("Zeilennummer-Breite", meta.ln_width),
        ("Dateigroesse", f"{meta.total_file_size} Bytes"),
        ("SHA-256", meta.sha256.hex()),
        ("Dateiname", meta.filename or "(keiner gespeichert)"),
    ]
    width = max(len(label) for label, _ in rows) + 1
    return "\n".join(f"  {label + ':':<{width}} {value}" for label, value in rows)


def print_headers(sources) -> bool:
    """--header: gibt fuer jede Quelle die Header aller darin gefundenen
    Seiten aus, ohne die Nutzdaten anzufassen. Liest nur die ASCII-Sicht.
    Rueckgabe: True, wenn jede Quelle mindestens einen lesbaren Header hatte
    und kein Header unlesbar war."""
    # Custom-Alphabete koennen Zeichen enthalten, die die Konsole nicht kann
    sys.stdout.reconfigure(errors="backslashreplace")
    header_alpha = Alphabet(HEADER_ALPHABET)
    all_ok = True
    for idx, (name, raw) in enumerate(sources):
        if idx:
            print()
        print(f"== {name} ==")
        lines = split_text_lines(decode_header_text(raw))
        positions = find_preamble_positions(lines, header_alpha)
        if not positions:
            print("  (keine gueltige Seiten-Praeambel gefunden)")
            all_ok = False
            continue
        for start in positions:
            try:
                meta, _ = parse_page_header(lines[start:], header_alpha)
            except HeaderError as e:
                print(f"  Zeile {start + 1}: Header nicht rekonstruierbar ({e})")
                all_ok = False
                continue
            if len(positions) > 1:
                print(f" -- Seite ab Zeile {start + 1} --")
            print(format_header(meta))
    return all_ok


@dataclass
class Page:
    meta: HeaderMeta
    rest_lines: list[str]  # Zeilen nach dem Header bis zur naechsten Praeambel (Nutzdaten-Sicht)


def read_pages(header_lines, payload_lines, source_ranges, header_alpha) -> list[Page]:
    """Pass 1: findet und parst alle Seiten-Header. Header-Fehler sind hier
    NICHT fatal fuer den ganzen Lauf - nur die betroffene Seite faellt aus."""
    preamble_positions = find_preamble_positions(header_lines, header_alpha)
    if not preamble_positions:
        raise FatalError("FEHLER (Praeambel): keine gueltige Seiten-Praeambel in der Eingabe gefunden.")

    pages = []
    ends = preamble_positions[1:] + [len(header_lines)]
    for start, end in zip(preamble_positions, ends):
        try:
            meta, expected_header_body = parse_page_header(header_lines[start:end], header_alpha)
        except HeaderError as e:
            print(f"Hinweis: Header einer Seite nicht rekonstruierbar ({e}) - diese Seite wird uebersprungen.",
                  file=sys.stderr)
            continue

        # Cross-Check: von dieser Seite deklariertes text_encoding-Feld muss
        # zum tatsaechlich per Byte-Sniffing erkannten Encoding ihrer Quelle
        # passen (siehe read_all_input_lines/sniff_layout). Ein Latin-1-
        # gelesener 8-Bit-Strom zaehlt dabei als UTF-8 (text_encoding=0
        # steht fuer "8-Bit-Strom"). Nicht fatal fuer den ganzen Lauf - wie
        # jeder andere Seiten-Header-Fehler auch.
        declared_encoding = TEXT_ENCODING_TO_CODEC[meta.text_encoding]
        sniffed_encoding = encoding_at(start, source_ranges)
        if declared_encoding != sniffed_encoding:
            print(
                f"Hinweis: Seite deklariert Encoding {declared_encoding!r}, wurde aber als "
                f"{sniffed_encoding!r} gelesen - diese Seite wird uebersprungen.",
                file=sys.stderr,
            )
            continue

        pages.append(Page(meta=meta, rest_lines=payload_lines[start + 1 + expected_header_body:end]))

    if not pages:
        raise FatalError("FEHLER (Header nicht rekonstruierbar): keine einzige Seite hatte einen lesbaren Header.")

    # Unterschiedliche Dokumente nicht stillschweigend mischen
    doc_ids = {p.meta.document_id for p in pages}
    if len(doc_ids) > 1:
        ids_hex = ", ".join(sorted(d.hex() for d in doc_ids))
        raise FatalError(
            f"FEHLER: Seiten mit unterschiedlichen Dokument-IDs in der Eingabe gemischt "
            f"({ids_hex}) - Abbruch, kein automatisches Aufteilen auf mehrere Dokumente."
        )
    return pages


# ---------------------------------------------------------------------------
# Nutzdatenzeilen
# ---------------------------------------------------------------------------

@dataclass
class DecodeContext:
    """Fuer alle Seiten eines Dokuments gemeinsame Parameter."""
    meta: HeaderMeta
    header_alpha: Alphabet
    payload_alpha: Alphabet = field(init=False)
    gf: GaloisField = field(init=False)
    crc_width: int = field(init=False)
    wide_crc: bool = field(init=False)
    total_lines: int = field(init=False)

    def __post_init__(self):
        m = self.meta
        self.payload_alpha = Alphabet(m.alphabet_chars) if m.is_custom_alphabet else Alphabet(DEFAULT_ALPHABET)
        self.gf = GaloisField(self.bits_per_symbol)
        self.crc_width = crc_width_for(self.header_alpha)
        self.wide_crc = crc_is_wide(m.version, self.bits_per_symbol)
        self.total_lines = total_lines_for(m.total_file_size, m.k, self.bits_per_symbol)

    @property
    def bits_per_symbol(self) -> int:
        return self.payload_alpha.bits_per_symbol

    @property
    def payload_line_width(self) -> int:
        return self.meta.ln_width + self.crc_width + self.meta.k + self.meta.r

    def parse(self, raw: str) -> LineResult | None:
        m = self.meta
        return parse_payload_line(raw, m.k, m.r, self.gf, m.ln_width, self.crc_width,
                                  self.payload_alpha, self.header_alpha, self.wide_crc)


@dataclass
class _Candidate:
    result: LineResult
    supported: bool  # direkter Nachbar in derselben Seite hat Zeilennummer +-1


@dataclass
class LineCollection:
    results: dict  # line_number -> LineResult (ueber ALLE Seiten aufgeloest)
    parse_errors: list  # LineResults ohne verwertbare Zeilennummer
    page_line_ranges: dict  # page_number -> (min_ln, max_ln) der plausiblen Zeilen dieser Seite
    total_rejoined: int


def collect_lines(pages: list[Page], ctx: DecodeContext) -> LineCollection:
    """Pass 2: Nutzdatenzeilen aller Seiten parsen und je Zeilennummer
    zusammenfuehren.

    Die Zeilennummer selbst ist nicht durch das CRC geschuetzt. Damit ein
    einzelner Lesefehler darin nicht andere Zeilen verdraengt, gilt:
    - Nummern ausserhalb 0..total_lines-1 sind sicher falsch -> nicht zuordenbar.
    - Eine Zeile gilt als "gestuetzt", wenn ein direkter Nachbar in derselben
      Seite die Nummer davor/danach traegt.
    - Gibt es fuer eine Nummer mehrere gueltige Zeilen mit VERSCHIEDENEN
      Daten, gewinnt die einzige gestuetzte; ist das nicht eindeutig, wird
      die Zeile als widerspruechlich gemeldet statt geraten.
    Doppelte Zeilen mit identischen Daten (z.B. ueberlappende Screenshots
    oder doppelt eingefuegte Seite) sind unproblematisch."""
    candidates = defaultdict(list)
    parse_errors = []
    page_line_ranges = {}
    total_rejoined = 0

    for page in pages:
        rest_lines, n_rejoined = reflow_joined_lines(page.rest_lines, ctx.payload_line_width)
        total_rejoined += n_rejoined

        parsed = []
        for raw in rest_lines:
            res = ctx.parse(raw)
            if res is None:
                continue  # komplett leere Zeile -> ignorieren
            if res.line_number is not None and res.line_number >= ctx.total_lines:
                res = LineResult(
                    None, res.status, reason=(
                        f"Zeilennummer {res.line_number} ausserhalb 0..{ctx.total_lines - 1} "
                        f"(Lesefehler im Zeilennummernfeld?)"
                    ),
                )
            if res.line_number is None:
                parse_errors.append(res)
            parsed.append(res)

        page_lns, supported_lns = [], []
        for j, res in enumerate(parsed):
            ln = res.line_number
            if ln is None:
                continue
            prev_ln = parsed[j - 1].line_number if j > 0 else None
            next_ln = parsed[j + 1].line_number if j + 1 < len(parsed) else None
            supported = prev_ln == ln - 1 or next_ln == ln + 1
            candidates[ln].append(_Candidate(res, supported))
            page_lns.append(ln)
            if supported:
                supported_lns.append(ln)
        range_lns = supported_lns or page_lns
        if range_lns:
            page_line_ranges[page.meta.page_number] = (min(range_lns), max(range_lns))

    results = {ln: _resolve(ln, cands) for ln, cands in candidates.items()}
    return LineCollection(results, parse_errors, page_line_ranges, total_rejoined)


def _resolve(ln: int, cands: list[_Candidate]) -> LineResult:
    """Waehlt aus allen Kandidaten fuer Zeilennummer `ln` das Ergebnis aus
    (siehe collect_lines)."""
    oks = [c for c in cands if c.result.status == LineStatus.OK]
    if not oks:
        return cands[0].result
    if len({tuple(c.result.data) for c in oks}) == 1:
        return min(oks, key=lambda c: (not c.supported, c.result.repaired is not None)).result
    supported = [c for c in oks if c.supported]
    if len({tuple(c.result.data) for c in supported}) == 1:
        return supported[0].result
    return LineResult(
        ln, LineStatus.CONFLICT,
        reason=f"{len(oks)} gueltige, aber verschiedene Zeilen mit dieser Nummer "
               f"(Lesefehler in einer Zeilennummer?)",
    )


@dataclass
class Assembly:
    full_bytes: bytes
    bad_lines: list  # (line_number, status, reason)
    repaired_lines: list  # (line_number, beschreibung) - per Laengenreparatur gerettet
    first_bad_line: int | None


def assemble(results: dict, ctx: DecodeContext) -> Assembly:
    """Setzt den vollstaendigen Symbolstrom zusammen (Luecken = 0-Symbole)."""
    k = ctx.meta.k
    full_symbols = [0] * (ctx.total_lines * k)
    bad_lines = []
    repaired_lines = []
    for i in range(ctx.total_lines):
        res = results.get(i)
        if res is None:
            bad_lines.append((i, LineStatus.MISSING.value, "Zeile in der Eingabe nicht gefunden"))
        elif res.status == LineStatus.OK:
            data = res.data[:k]
            if ctx.meta.scrambled:
                data = scramble_block(data, i, ctx.meta.document_id, ctx.bits_per_symbol)
            full_symbols[i * k:(i + 1) * k] = data
            if res.repaired:
                repaired_lines.append((i, res.repaired))
        else:
            # 'fragwuerdig_korrigiert' Daten NICHT in den Hauptstrom uebernehmen,
            # nur im Fehlerbericht melden.
            bad_lines.append((i, res.status.value, res.reason))
    full_bytes = ctx.payload_alpha.symbols_to_bytes(full_symbols, expected_length=ctx.meta.total_file_size)
    first_bad_line = bad_lines[0][0] if bad_lines else None
    return Assembly(full_bytes, bad_lines, repaired_lines, first_bad_line)


def verify_pages(pages: list[Page], page_line_ranges: dict, full_bytes: bytes, ctx: DecodeContext):
    """Seiten-Pruefsummen verifizieren (zusaetzlich zu CRC/RS pro Zeile).
    Der Byte-Bereich einer Seite wird EMPIRISCH aus den auf dieser Seite
    gefundenen Zeilennummern abgeleitet (kein eigenes "Zeilen pro Seite"-
    Feld noetig). Fehlt die erste/letzte Zeile einer Seite, wird der Bereich
    etwas zu eng geschaetzt - die Pruefsumme schlaegt dann zwar an, aber das
    ist redundant zur ohnehin schon erkannten fehlenden Zeile, nie ein
    falsches "OK". Rueckgabe: Liste (page_number, grund)."""
    bad_pages = []
    for page in pages:
        pn = page.meta.page_number
        rng = page_line_ranges.get(pn)
        if rng is None:
            bad_pages.append((pn, "keine gueltige Nutzdatenzeile auf dieser Seite gefunden"))
            continue
        min_ln, max_ln = rng
        b0 = byte_offset_of_line(min_ln, ctx.meta.k, ctx.bits_per_symbol)
        b1 = min(byte_offset_of_line(max_ln + 1, ctx.meta.k, ctx.bits_per_symbol), len(full_bytes))
        if zlib.crc32(full_bytes[b0:b1]) != page.meta.page_checksum:
            bad_pages.append((pn, "Seiten-Pruefsumme stimmt nicht (Seite vermutlich unvollstaendig/beschaedigt)"))
    return bad_pages


# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------

def report_path(args) -> str:
    ext = "json" if args.error_report_format == "json" else "txt"
    return f"{args.output}.errors.{ext}"


def write_report(args, status, hash_status, bad_lines, parse_errors, total_lines, filename,
                 page_count, missing_pages, bad_pages, repaired_lines):
    path = report_path(args)
    if args.error_report_format == "json":
        payload = dict(
            status=status, filename=filename, total_lines=total_lines,
            hash_status=hash_status,
            page_count=page_count, missing_pages=missing_pages,
            bad_pages=[dict(page=pn, reason=reason) for pn, reason in bad_pages],
            bad_lines=[dict(line=ln, status=st, reason=reason) for ln, st, reason in bad_lines],
            repaired_lines=[dict(line=ln, repair=desc) for ln, desc in repaired_lines],
            unparseable_lines=[dict(status=r.status.value, reason=r.reason) for r in parse_errors],
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Status: {status}\n")
        f.write(f"Dateiname (aus Header): {filename or '(keiner gespeichert)'}\n")
        f.write(f"Zeilen gesamt: {total_lines}\n")
        f.write(f"Seiten gesamt: {page_count}\n")
        f.write(f"SHA-256-Pruefung: {hash_status}\n")
        if missing_pages or bad_pages:
            f.write("\nSeiten mit Problemen:\n")
            for pn in missing_pages:
                f.write(f"  Seite {pn}: komplett fehlend (kein lesbarer Header gefunden)\n")
            for pn, reason in bad_pages:
                f.write(f"  Seite {pn}: {reason}\n")
        f.write(f"\nFehlerhafte/fehlende Zeilen ({len(bad_lines)}):\n")
        for ln, st, reason in bad_lines:
            f.write(f"  Zeile {ln}: {st} - {reason}\n")
        if repaired_lines:
            f.write(f"\nPer Laengenreparatur gerettete Zeilen ({len(repaired_lines)}):\n")
            for ln, desc in repaired_lines:
                f.write(f"  Zeile {ln}: {desc}\n")
        if parse_errors:
            f.write(f"\nNicht zuordenbare Zeilen ({len(parse_errors)}):\n")
            for r in parse_errors:
                f.write(f"  {r.status.value} - {r.reason}\n")


def decode(args) -> int:
    sources = read_sources(args.input)
    if args.header:
        return EXIT_OK if print_headers(sources) else EXIT_FATAL

    header_lines, payload_lines, source_ranges = read_all_input_lines(sources)
    if not any(line.strip() for line in header_lines):
        raise FatalError("FEHLER: Eingabe ist komplett leer.")

    header_alpha = Alphabet(HEADER_ALPHABET)
    pages = read_pages(header_lines, payload_lines, source_ranges, header_alpha)

    # Gemeinsame Felder: identisch auf jeder Seite, also reicht eine beliebige.
    meta = pages[0].meta
    ctx = DecodeContext(meta, header_alpha)

    seen_page_numbers = sorted(p.meta.page_number for p in pages)
    if meta.page_count > 1 or len(pages) > 1:
        print(f"Seiten gelesen: {len(pages)} von {meta.page_count} (Nummern: {seen_page_numbers}).",
              file=sys.stderr)

    lines = collect_lines(pages, ctx)
    if lines.total_rejoined:
        # Clipboard-Transfer (statt OCR) bei grossen Dateien braucht mehrere
        # Copy/Paste-Vorgaenge; an der Nahtstelle geht dabei gelegentlich ein
        # Zeilenumbruch verloren (siehe framing.reflow_joined_lines).
        print(
            f"Hinweis: {lines.total_rejoined} Roh-Zeile(n) enthielten offenbar mehrere zusammengefuegte "
            f"Nutzdatenzeilen (Zeilenumbruch-Verlust, z.B. Zwischenablage-Artefakt) und wurden "
            f"anhand der bekannten Zeilenbreite ({ctx.payload_line_width} Symbole) automatisch wieder "
            f"aufgeteilt.",
            file=sys.stderr,
        )

    asm = assemble(lines.results, ctx)
    missing_pages = sorted(set(range(meta.page_count)) - set(seen_page_numbers))
    bad_pages = verify_pages(pages, lines.page_line_ranges, asm.full_bytes, ctx)

    if asm.repaired_lines:
        print(
            f"Hinweis: {len(asm.repaired_lines)} Zeile(n) mit falsch gezaehlten Folgen gleicher Zeichen "
            f"per Laengenreparatur gerettet (Details im Fehlerbericht).",
            file=sys.stderr,
        )

    report_kwargs = dict(
        parse_errors=lines.parse_errors, total_lines=ctx.total_lines, filename=meta.filename,
        page_count=meta.page_count, missing_pages=missing_pages, bad_pages=bad_pages,
        repaired_lines=asm.repaired_lines,
    )

    if asm.first_bad_line is None:
        # Alles sauber -> ein einziges vollstaendiges Ausgabefile.
        hash_ok = hashlib.sha256(asm.full_bytes).digest() == meta.sha256
        hash_status = "OK" if hash_ok else "FEHLGESCHLAGEN"
        with open(args.output, "wb") as f:
            f.write(asm.full_bytes)
        write_report(args, status="vollstaendig", hash_status=hash_status, bad_lines=[], **report_kwargs)
        if hash_ok:
            print(f"OK: vollstaendig rekonstruiert -> {args.output} (Hash-Pruefung: OK)", file=sys.stderr)
            return EXIT_OK
        print(
            f"FEHLER: alle Zeilen gelesen -> {args.output}, aber die SHA-256-Pruefung ist FEHLGESCHLAGEN "
            f"(Datei ist nicht identisch mit dem Original, siehe {report_path(args)}).",
            file=sys.stderr,
        )
        return EXIT_HASH_MISMATCH

    split_at = byte_offset_of_line(asm.first_bad_line, meta.k, ctx.bits_per_symbol)
    part1, part2 = asm.full_bytes[:split_at], asm.full_bytes[split_at:]
    p1_path, p2_path = f"{args.output}.part1", f"{args.output}.part2"
    with open(p1_path, "wb") as f:
        f.write(part1)
    with open(p2_path, "wb") as f:
        f.write(part2)
    write_report(args, status="unvollstaendig", hash_status="uebersprungen (unvollstaendig)",
                 bad_lines=asm.bad_lines, **report_kwargs)
    page_note = f", {len(missing_pages)} Seite(n) komplett fehlend" if missing_pages else ""
    print(
        f"UNVOLLSTAENDIG: {len(asm.bad_lines)} von {ctx.total_lines} Zeilen fehlerhaft/fehlend{page_note}.\n"
        f"  {p1_path} (sauberer Anfang, {len(part1)} Bytes)\n"
        f"  {p2_path} (Rest inkl. Luecken, {len(part2)} Bytes)\n"
        f"  {report_path(args)} (Fehlerbericht)",
        file=sys.stderr,
    )
    return EXIT_INCOMPLETE


def main() -> int:
    ap = argparse.ArgumentParser(description="Air-Gap Text-Decoder mit Reed-Solomon-Fehlerkorrektur")
    ap.add_argument(
        "input", nargs="*",
        help=(
            "OCR-Text-/Seiten-Datei(en) (Default: stdin). Mehrere Dateien und/oder "
            "Glob-Patterns (z.B. 'seite_*.txt') sind erlaubt - alle werden zusammen "
            "nach Seiten durchsucht, unabhaengig von der Reihenfolge."
        ),
    )
    ap.add_argument("-o", "--output", default="output", help="Basisname der Ausgabedatei(en) (Default: 'output')")
    ap.add_argument("--error-report-format", choices=["text", "json"], default="text",
                    help="Format des Fehlerberichts (Default: text)")
    ap.add_argument(
        "--header", action="store_true",
        help="Nur die Header aller Seiten jeder Eingabedatei ausgeben, Nutzdaten nicht verarbeiten",
    )
    args = ap.parse_args()
    try:
        return decode(args)
    except FatalError as e:
        print(e, file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
