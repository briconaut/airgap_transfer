"""
Nutzdatenzeile:  [Zeilennummer: ln_width Symbole, Header-Alphabet]
                 [CRC32:        crc_width Symbole, Header-Alphabet]
                 [Nutzteil:     interleave * (k+r) Symbole, Nutzdaten-Alphabet]

Der Nutzteil besteht aus `interleave` Reed-Solomon-Codewoertern (je k Daten-
und r Paritaetssymbole), deren Symbole reihum verschraenkt sind: Position j
gehoert zu Codewort j % interleave (siehe LineCode). Ein RS-Codewort kann
hoechstens Alphabetgroesse-1 Symbole lang sein; durch mehrere Codewoerter
je Zeile ist die Zeilenbreite davon unabhaengig. Die Verschraenkung verteilt
benachbarte Lesefehler zudem auf verschiedene Codewoerter.

CRC32 wird NUR ueber die interleave*k Nutzdatensymbole der Zeile berechnet
(nicht ueber die Zeilennummer - bewusste Design-Entscheidung, siehe README
"Restrisiken").
"""
import re
import zlib
from dataclasses import dataclass
from enum import Enum

from .alphabets import Alphabet
from .rs_codec import GaloisField, rs_decode, rs_encode, rs_generator_poly
from .util import ceil_div, digits_needed, digits_to_int, int_to_digits

CRC_BITS = 32
DEFAULT_REDUNDANCY_PCT = 20.0


def crc_width_for(header_alpha: Alphabet) -> int:
    return ceil_div(CRC_BITS, header_alpha.bits_per_symbol)


def symbols_crc32(symbols, wide: bool) -> int:
    """CRC32 ueber Datensymbole. wide=False: 1 Byte pro Symbol (nur das
    untere Byte - so bis Formatversion 4, bei <= 8 Bit/Symbol verlustfrei).
    wide=True: Symbole > 8 Bit (GF512) werden als 2 Byte big-endian erfasst,
    damit das CRC auch das oberste Bit sieht (ab Formatversion 5). Welche
    Variante gilt, entscheidet Format.crc_is_wide (siehe formats/)."""
    if wide:
        data = b"".join(s.to_bytes(2, "big") for s in symbols)
    else:
        data = bytes(s & 0xFF for s in symbols)
    return zlib.crc32(data) & 0xFFFFFFFF


def byte_offset_of_line(line_no: int, k: int, bits_per_symbol: int) -> int:
    """Byte-Offset in der Originaldatei, an dem Nutzdatenzeile `line_no`
    beginnt (Floor-Trick, siehe symbols_to_bytes in alphabets.py). Gemeinsam
    genutzt von encode.py (Seiten-Pruefsumme) und decode.py (part1/part2-
    Schnitt + Seiten-Pruefsumme) - beide muessen exakt dieselbe Formel
    verwenden, sonst passen die CRC32-Pruefsummen nicht zusammen."""
    return (line_no * k * bits_per_symbol) // 8


def total_lines_for(total_file_size: int, k: int, bits_per_symbol: int) -> int:
    """Anzahl Nutzdatenzeilen fuer eine Datei dieser Groesse (mindestens 1,
    auch eine leere Datei belegt eine - mit Nullen aufgefuellte - Zeile)."""
    total_data_symbols = ceil_div(total_file_size * 8, bits_per_symbol)
    return max(1, ceil_div(total_data_symbols, k))


@dataclass(frozen=True)
class Layout:
    k: int  # Datensymbole je Codewort
    r: int  # Paritaetssymbole je Codewort
    interleave: int  # Codewoerter je Zeile
    ln_width: int
    crc_width: int
    total_lines: int
    width_clamped: bool = False  # --width wurde auf das Maximum der Formatversion gekuerzt

    @property
    def data_per_line(self) -> int:
        return self.k * self.interleave

    @property
    def effective_width(self) -> int:
        """Tatsaechliche Laenge einer Nutzdatenzeile in Zeichen (<= --width;
        kleiner nur um den Rest, der sich nicht gleichmaessig auf die
        Codewoerter verteilen laesst)."""
        return self.ln_width + self.crc_width + self.interleave * (self.k + self.r)


def compute_layout(width: int, total_data_symbols: int, payload_alpha: Alphabet,
                   header_alpha: Alphabet, redundancy_pct: float | None,
                   parity_symbols: int | None, max_interleave: int | None = None,
                   clamp_width: bool = False) -> Layout:
    """Loest iterativ k, r, interleave, ln_width aus --width + --redundancy
    (oder --parity-symbols). parity_symbols ist die Paritaet JE ZEILE und wird
    (aufgerundet) gleichmaessig auf die Codewoerter verteilt.
    ln_width haengt von total_lines ab, total_lines haengt von k ab, k haengt
    (ueber die Restbreite) von ln_width ab - konvergiert aber in 1-2
    Iterationsschritten, da ln_width sich nur logarithmisch aendert.
    max_interleave: Hoechstzahl Codewoerter je Zeile (Formatversionen < 6: 1,
    None = beliebig). Passt --width damit nicht, wird bei clamp_width auf die
    groesstmoegliche Breite gekuerzt (Layout.width_clamped), sonst ValueError."""
    cw = crc_width_for(header_alpha)
    max_n = payload_alpha.size - 1  # RS-Codewortlaenge <= Feldgroesse - 1
    pct = DEFAULT_REDUNDANCY_PCT if redundancy_pct is None else redundancy_pct
    if parity_symbols is None and pct <= 0:
        raise ValueError(f"--redundancy muss > 0 sein, nicht {pct}")

    max_remaining = None if max_interleave is None else max_interleave * max_n
    ln_width = 1
    for _ in range(20):
        remaining = width - cw - ln_width
        if remaining < 2:
            raise ValueError(
                f"--width {width} ist zu klein (Header-Alphabet-Felder allein "
                f"brauchen schon {cw + ln_width} Symbole)"
            )
        if max_remaining is not None:
            remaining = min(remaining, max_remaining)
        interleave = ceil_div(remaining, max_n)
        n = remaining // interleave  # Laenge jedes Codeworts (<= max_n)
        if parity_symbols is not None:
            r = ceil_div(parity_symbols, interleave)
            k = n - r
        else:
            k = max(1, round(n / (1 + pct / 100.0)))
            r = n - k
        if k < 1 or r < 1:
            raise ValueError(
                f"--width {width} reicht nicht fuer sinnvolle k/r-Werte (k={k}, r={r} je Codewort)"
            )

        total_lines = max(1, ceil_div(total_data_symbols, k * interleave))
        new_ln_width = digits_needed(total_lines, header_alpha.size)
        if new_ln_width == ln_width:
            break
        ln_width = new_ln_width
    else:
        raise ValueError("Layout-Berechnung konvergiert nicht (unerwartet)")

    # erst nach der Konvergenz pruefen, ln_width steht dann fest
    clamped = max_remaining is not None and width - cw - ln_width > max_remaining
    if clamped and not clamp_width:
        raise ValueError(
            f"--width {width} ist fuer diese Formatversion zu gross "
            f"(maximal {cw + ln_width + max_remaining} bei diesem Alphabet und dieser Dateigroesse)"
        )

    return Layout(k=k, r=r, interleave=interleave, ln_width=ln_width, crc_width=cw,
                  total_lines=total_lines, width_clamped=clamped)


class LineCode:
    """Fehlerkorrektur einer Nutzdatenzeile: `interleave` RS-Codewoerter mit
    je k Daten- und r Paritaetssymbolen. Die Daten der Zeile werden der Reihe
    nach auf die Codewoerter verteilt (Codewort i bekommt data[i*k:(i+1)*k]),
    die Codewoerter dann symbolweise reihum verschraenkt."""

    def __init__(self, k: int, r: int, interleave: int, gf: GaloisField):
        self.k, self.r, self.interleave, self.gf = k, r, interleave, gf
        self.gen = rs_generator_poly(r, gf)

    @property
    def data_len(self) -> int:
        return self.k * self.interleave

    @property
    def encoded_len(self) -> int:
        return (self.k + self.r) * self.interleave

    def encode(self, data) -> list[int]:
        if len(data) != self.data_len:
            raise ValueError(f"Datenblock hat {len(data)} statt {self.data_len} Symbole")
        k, il = self.k, self.interleave
        codewords = [rs_encode(data[i * k:(i + 1) * k], self.r, self.gen, self.gf) for i in range(il)]
        return [codewords[j % il][j // il] for j in range(self.encoded_len)]

    def decode(self, symbols):
        """Rueckgabe: (daten, anzahl_korrigierter_fehler) oder (None, -1),
        sobald eines der Codewoerter nicht korrigierbar ist."""
        if len(symbols) != self.encoded_len:
            return None, -1
        data, total_errors = [], 0
        for i in range(self.interleave):
            decoded, num_errors = rs_decode(symbols[i::self.interleave], self.r, self.gf)
            if decoded is None:
                return None, -1
            data.extend(decoded)
            total_errors += num_errors
        return data, total_errors


def build_payload_line(line_number: int, data_block, code: LineCode, ln_width: int, crc_width: int,
                       payload_alpha: Alphabet, header_alpha: Alphabet, wide_crc: bool) -> str:
    """wide_crc: siehe Format.crc_is_wide."""
    encoded = code.encode(data_block)
    crc = symbols_crc32(data_block, wide_crc)
    ln_digits = int_to_digits(line_number, ln_width, header_alpha.size)
    crc_digits = int_to_digits(crc, crc_width, header_alpha.size)
    return header_alpha.symbols_to_text(ln_digits + crc_digits) + payload_alpha.symbols_to_text(encoded)


class LineStatus(str, Enum):
    OK = "ok"
    CORRECTED_SUSPECT = "fragwuerdig_korrigiert"  # RS "loeste" was, aber CRC passt danach nicht
    UNCORRECTABLE = "verworfen"
    CONFLICT = "widerspruechlich"  # mehrere gueltige, aber verschiedene Zeilen mit derselben Nummer
    MISSING = "fehlend"


@dataclass
class LineResult:
    """Ergebnis von parse_payload_line fuer eine Roh-Zeile."""
    line_number: int | None  # None, wenn schon das Praefix unlesbar war
    status: LineStatus
    data: list[int] | None = None
    reason: str | None = None
    repaired: str | None = None  # Beschreibung einer Laengenreparatur, sonst None


def parse_payload_line(raw: str, code: LineCode, ln_width: int, crc_width: int,
                       payload_alpha: Alphabet, header_alpha: Alphabet,
                       wide_crc: bool) -> LineResult | None:
    """Parst eine Roh-Zeile. Rueckgabe None fuer eine komplett leere Zeile
    (vom Aufrufer wie eine Luecke zu behandeln), sonst ein LineResult.
    wide_crc: siehe Format.crc_is_wide."""
    raw = raw.strip()
    if not raw:
        return None

    prefix_len = ln_width + crc_width
    prefix_raw, payload_raw = raw[:prefix_len], raw[prefix_len:]

    try:
        prefix_symbols = header_alpha.text_to_symbols(prefix_raw)
    except ValueError:
        return LineResult(None, LineStatus.UNCORRECTABLE, reason="Praefix nicht im Header-Alphabet lesbar")

    if len(prefix_symbols) != prefix_len:
        return LineResult(None, LineStatus.UNCORRECTABLE, reason="Zeile zu kurz fuer Praefix")

    line_number = digits_to_int(prefix_symbols[:ln_width], header_alpha.size)
    expected_crc = digits_to_int(prefix_symbols[ln_width:], header_alpha.size)

    try:
        payload_symbols = payload_alpha.text_to_symbols(payload_raw)
    except ValueError:
        return LineResult(line_number, LineStatus.UNCORRECTABLE,
                          reason="Nutzdatenteil nicht im Nutzdaten-Alphabet lesbar")

    if len(payload_symbols) == code.encoded_len:
        status, decoded, reason = _decode_payload(payload_symbols, code, expected_crc, wide_crc)
        if status == LineStatus.OK:
            return LineResult(line_number, status, data=decoded)
    else:
        status, decoded, reason = (LineStatus.UNCORRECTABLE, None,
                                   f"Erwartet {code.encoded_len} Symbole, {len(payload_symbols)} vorhanden")

    repair = _repair_shifted_payload(payload_raw, code, expected_crc, payload_alpha, wide_crc)
    if repair is not None:
        decoded, description = repair
        return LineResult(line_number, LineStatus.OK, data=decoded, repaired=description)
    return LineResult(line_number, status, data=decoded, reason=reason)


def _decode_payload(payload_symbols, code: LineCode, expected_crc: int, wide_crc: bool):
    """RS-Korrektur aller Codewoerter + CRC-Pruefung des Nutzteils einer Zeile.
    Rueckgabe: (status, daten oder None, grund oder None)."""
    decoded, num_errors = code.decode(payload_symbols)
    if decoded is None:
        return LineStatus.UNCORRECTABLE, None, "Reed-Solomon: zu viele Fehler in dieser Zeile"
    if symbols_crc32(decoded, wide_crc) != expected_crc:
        return (LineStatus.CORRECTED_SUSPECT, decoded,
                f"RS meldet {num_errors} korrigierte Fehler, aber CRC passt danach nicht")
    return LineStatus.OK, decoded, None


# Groesste Laengenabweichung (in Zeichen), die _repair_shifted_payload noch
# zu reparieren versucht. Auch reflow_joined_lines toleriert Zeilen, die um
# hoechstens so viel zu lang sind, statt sie aufzuteilen.
MAX_SHIFT_REPAIR = 2


def _runs(text: str):
    """Folgen gleicher Zeichen: Liste von (startindex, laenge)."""
    runs = []
    i = 0
    while i < len(text):
        j = i
        while j < len(text) and text[j] == text[i]:
            j += 1
        runs.append((i, j - i))
        i = j
    return runs


def _shift_candidates(text: str, delta: int):
    """Erzeugt Varianten von `text`, deren Laenge um `delta` korrigiert ist
    (delta > 0: Zeichen zu viel, delta < 0: zu wenig, delta == 0: eine Folge
    zu lang UND eine andere zu kurz). Repariert wird nur innerhalb von Folgen
    gleicher Zeichen - genau dort verzaehlt sich OCR typischerweise.
    Entfernt wird nur aus Folgen >= 2; eingefuegt wird in jede Folge, auch
    in Einzelzeichen (eine "VV" als "V" gelesen ist danach eine 1er-Folge).
    Rueckgabe: Iterator ueber (kandidat, ops) mit ops = Liste von
    (startindex der Folge, +1 eingefuegt / -1 entfernt)."""
    runs = _runs(text)
    run_len = dict(runs)
    all_runs = [start for start, _ in runs]
    long_runs = [start for start, length in runs if length >= 2]

    if delta == 1:
        op_sets = [[(p, -1)] for p in long_runs]
    elif delta == -1:
        op_sets = [[(p, +1)] for p in all_runs]
    elif delta == 2:
        op_sets = [[(p, -1), (q, -1)] for i, p in enumerate(long_runs) for q in long_runs[i + 1:]]
        op_sets += [[(p, -1), (p, -1)] for p in long_runs if run_len[p] >= 3]
    elif delta == -2:
        op_sets = [[(p, +1), (q, +1)] for i, p in enumerate(all_runs) for q in all_runs[i:]]
    elif delta == 0:
        op_sets = [[(p, -1), (q, +1)] for p in long_runs for q in all_runs if p != q]
    else:
        op_sets = []

    for ops in op_sets:
        candidate = text
        # von hinten nach vorn anwenden, damit die vorderen Indizes gueltig bleiben
        for pos, change in sorted(ops, reverse=True):
            if change < 0:
                candidate = candidate[:pos] + candidate[pos + 1:]
            else:
                candidate = candidate[:pos] + candidate[pos] + candidate[pos:]
        yield candidate, ops


def _repair_shifted_payload(payload_raw: str, code: LineCode, expected_crc: int,
                            payload_alpha: Alphabet, wide_crc: bool):
    """Versucht eine Zeile zu retten, in der die OCR eine Folge gleicher
    Zeichen falsch gezaehlt hat: Ein einziges fehlendes/zusaetzliches Zeichen
    verschiebt den Rest der Zeile, sodass RS weit mehr als floor(r/2)
    Fehler sieht. Probiert die Varianten aus _shift_candidates durch und
    akzeptiert nur eine, die RS dekodieren kann UND deren CRC32 stimmt - bei
    der Vielzahl an Kandidaten ist der CRC die eigentliche Absicherung gegen
    Falsch-Positive. Bei mehreren Treffern gewinnt der mit den wenigsten
    RS-Korrekturen. Rueckgabe: (daten, beschreibung) oder None."""
    delta = len(payload_raw) - code.encoded_len
    if abs(delta) > MAX_SHIFT_REPAIR:
        return None
    best = None
    seen = set()
    for candidate, ops in _shift_candidates(payload_raw, delta):
        if candidate in seen:
            continue
        seen.add(candidate)
        decoded, num_errors = code.decode(payload_alpha.text_to_symbols(candidate))
        if decoded is None or symbols_crc32(decoded, wide_crc) != expected_crc:
            continue
        if best is None or num_errors < best[1]:
            best = (decoded, num_errors, ops)
            if num_errors == 0:
                break
    if best is None:
        return None
    decoded, num_errors, ops = best
    parts = [
        f"'{payload_raw[pos]}' {'eingefuegt' if change > 0 else 'entfernt'} (Folge ab Position {pos})"
        for pos, change in ops
    ]
    return decoded, "; ".join(parts) + f", danach {num_errors} RS-Korrektur(en)"


def reflow_joined_lines(lines, expected_width: int):
    """Manche Uebertragungswege (z.B. Windows-Zwischenablage bei grossen Dateien,
    die mehrere Copy/Paste-Vorgaenge brauchen) verlieren gelegentlich den
    Zeilenumbruch an der Nahtstelle zwischen zwei Einfuegungen - dadurch
    verschmelzen zwei oder mehr Nutzdatenzeilen zu einer Roh-Zeile.

    Jede echte Nutzdatenzeile ist exakt `expected_width` Symbole lang (siehe
    build_payload_line). Eine Roh-Zeile, die laenger als `expected_width` ist,
    wird deshalb in Stuecke dieser Laenge zerlegt; ein nicht glatt teilbarer
    Rest wird als eigene (dann zu kurze) Zeile angehaengt statt verworfen.
    Jedes Stueck traegt seine eigene Zeilennummer im Praefix, daher ist die
    Reihenfolge der Stuecke fuer die weitere Verarbeitung irrelevant - ein
    falsch geratener Trennpunkt fuehrt hoechstens zu einer zusaetzlichen
    Kandidatenzeile, die an CRC/Reed-Solomon scheitert und verworfen wird
    (siehe LineStatus.UNCORRECTABLE / CORRECTED_SUSPECT), nie zu verlorenen
    echten Zeilen.

    Rueckgabe: (neue Zeilenliste, Anzahl der Roh-Zeilen, die aufgeteilt wurden).
    """
    if expected_width <= 0:
        return lines, 0
    out = []
    n_split = 0
    for raw in lines:
        s = raw.strip()
        # Bis MAX_SHIFT_REPAIR zu lange Zeilen sind eher verzaehlte Folgen
        # gleicher Zeichen als verschmolzene Zeilen (siehe parse_payload_line).
        if len(s) > expected_width + MAX_SHIFT_REPAIR:
            n = len(s) // expected_width
            out.extend(s[i * expected_width:(i + 1) * expected_width] for i in range(n))
            rest = s[n * expected_width:]
            if rest:
                out.append(rest)
            n_split += 1
        else:
            out.append(raw)
    return out, n_split


COLUMN_SEPARATOR = " ### "
# Beim Lesen tolerant: jede von Whitespace umschlossene '#'-Folge trennt
# Spalten (OCR macht aus ' ### ' gern ' ## ' oder verschluckt Leerzeichen
# am Rand). Eindeutig, da kein Alphabet Whitespace enthalten darf (siehe
# Alphabet.__init__) - eine Zeile ohne Spalten enthaelt also nie ' # '.
_COLUMN_SPLIT_RE = re.compile(r"\s+#+\s+")


def arrange_columns(lines, columns: int, col_width: int):
    """Verteilt die Nutzdatenzeilen EINER Seite spaltenweise (erst Spalte 1
    von oben nach unten, dann Spalte 2, ...) auf `columns` Spalten der Breite
    `col_width`, getrennt durch COLUMN_SEPARATOR. Kuerzere Zeilen werden mit
    Leerzeichen aufgefuellt; die letzte Zelle einer Zeile nicht (keine
    Leerzeichen am Zeilenende). Spaltenweise statt zeilenweise, damit ein
    OCR-Tool, das Spalten erkennt und nacheinander ausgibt, wieder die
    Originalreihenfolge liefert. Praeambel und Header gehoeren nicht hierher,
    sie stehen einspaltig davor (siehe unarrange_columns)."""
    if columns <= 1 or not lines:
        return list(lines)
    rows = ceil_div(len(lines), columns)
    out = []
    for row in range(rows):
        cells = lines[row::rows]
        cells = [c.ljust(col_width) for c in cells[:-1]] + [cells[-1]]
        out.append(COLUMN_SEPARATOR.join(cells))
    return out


def unarrange_columns(lines):
    """Umkehrung von arrange_columns ohne Kenntnis der Spaltenzahl: jeder
    zusammenhaengende Block nicht-leerer Zeilen (= eine Seite, Seiten sind
    durch Leerzeilen getrennt) wird an den Spaltentrennern zerlegt und
    spaltenweise wieder zu einer einzigen Zeilenfolge zusammengesetzt.
    Einspaltige Zeilen am Blockanfang (Praeambel, Header) landen dabei von
    selbst vor den Nutzdaten, da sie nur eine Zelle (Spalte 1) haben.
    Leerzeilen bleiben erhalten; Bloecke ohne Trenner bleiben unveraendert.
    Rueckgabe: (neue Zeilenliste, Anzahl mehrspaltiger Bloecke)."""
    out = []
    n_blocks = 0
    block = []

    def flush():
        nonlocal n_blocks
        cells = [_COLUMN_SPLIT_RE.split(line) for line in block]
        if any(len(c) > 1 for c in cells):
            n_blocks += 1
            for col in range(max(len(c) for c in cells)):
                out.extend(c[col] for c in cells if col < len(c))
        else:
            out.extend(block)
        block.clear()

    for line in lines:
        if line.strip():
            block.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return out, n_blocks
