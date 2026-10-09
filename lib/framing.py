"""
Nutzdatenzeile:  [Zeilennummer: ln_width Symbole, Header-Alphabet]
                 [CRC32:        crc_width Symbole, Header-Alphabet]
                 [RS-Codewort:  k+r Symbole, Nutzdaten-Alphabet]  (=Payload+Paritaet)

CRC32 wird NUR ueber die k Nutzdatensymbole berechnet (nicht ueber die
Zeilennummer - bewusste Design-Entscheidung, siehe Grilling-Runde 12).
"""
import math
import re
import zlib
from alphabets import Alphabet
from rs_codec import GaloisField, rs_generator_poly, rs_encode, rs_decode

CRC_BITS = 32


def crc_width_for(header_alpha: Alphabet) -> int:
    return math.ceil(CRC_BITS / header_alpha.bits_per_symbol)


def byte_offset_of_line(line_no: int, k: int, bits_per_symbol: int) -> int:
    """Byte-Offset in der Originaldatei, an dem Nutzdatenzeile `line_no`
    beginnt (Floor-Trick, siehe symbols_to_bytes in alphabets.py). Gemeinsam
    genutzt von encode.py (Seiten-Pruefsumme) und decode.py (part1/part2-
    Schnitt + Seiten-Pruefsumme) - beide muessen exakt dieselbe Formel
    verwenden, sonst passen die CRC32-Pruefsummen nicht zusammen."""
    return (line_no * k * bits_per_symbol) // 8


def compute_layout(width: int, total_data_symbols: int, payload_alpha: Alphabet,
                    header_alpha: Alphabet, redundancy_pct: float | None, parity_symbols: int | None):
    """Loest iterativ k, r, ln_width aus --width + --redundancy (oder --parity-symbols).
    Siehe Design-Notizen Runde 6/9: ln_width haengt von total_lines ab, total_lines
    haengt von k ab, k haengt (ueber die Restbreite) von ln_width ab - konvergiert
    aber in 1-2 Iterationsschritten, da ln_width sich nur logarithmisch aendert."""
    cw = crc_width_for(header_alpha)
    max_n = payload_alpha.size - 1  # RS-Codewortlaenge <= Feldgroesse - 1

    ln_width = 1
    for _ in range(20):
        remaining = width - cw - ln_width
        if remaining < 2:
            raise ValueError(
                f"--width {width} ist zu klein (Header-Alphabet-Felder allein "
                f"brauchen schon {cw + ln_width} Symbole)"
            )
        if parity_symbols is not None:
            r = parity_symbols
            k = remaining - r
        else:
            pct = redundancy_pct if redundancy_pct is not None else 20.0
            k = max(1, round(remaining / (1 + pct / 100.0)))
            r = remaining - k
        if k < 1 or r < 1:
            raise ValueError(
                f"--width {width} reicht nicht fuer sinnvolle k/r-Werte (k={k}, r={r})"
            )
        capped = False
        if k + r > max_n:
            # RS-Codewort kann bei diesem Alphabet nicht so lang werden wie --width
            # es erlauben wuerde (max. Codewortlaenge = Alphabetgroesse - 1). Wir
            # kappen k+r auf das Maximum statt hart abzubrechen.
            if parity_symbols is not None:
                # explizit vom Nutzer vorgegebenes r respektieren, nur k kuerzen
                if parity_symbols >= max_n:
                    raise ValueError(
                        f"--parity-symbols {parity_symbols} ist allein schon >= der "
                        f"maximalen RS-Codewortlaenge {max_n} - groesseres Alphabet noetig"
                    )
                r = parity_symbols
                k = max_n - r
            else:
                ratio = k / (k + r)
                k = max(1, round(max_n * ratio))
                r = max_n - k
            capped = True

        total_lines = max(1, math.ceil(total_data_symbols / k))
        new_ln_width = max(1, _digits_needed(total_lines, header_alpha.size))
        if new_ln_width == ln_width:
            break
        ln_width = new_ln_width
    else:
        raise ValueError("Layout-Berechnung konvergiert nicht (unerwartet)")

    effective_width = cw + ln_width + k + r
    return dict(k=k, r=r, ln_width=ln_width, crc_width=cw, total_lines=total_lines,
                capped=capped, effective_width=effective_width)


def _digits_needed(count: int, base: int) -> int:
    """Anzahl Symbole, um Werte 0..count-1 darzustellen."""
    if count <= 1:
        return 1
    d = 0
    v = count - 1
    while True:
        d += 1
        v //= base
        if v == 0:
            return d


def build_payload_line(line_number: int, data_block, k: int, r: int, gen, gf: GaloisField,
                         ln_width: int, crc_width: int,
                         payload_alpha: Alphabet, header_alpha: Alphabet) -> str:
    assert len(data_block) == k
    codeword = rs_encode(data_block, r, gen, gf)  # k Daten- + r Paritaetssymbole

    crc = zlib.crc32(bytes(data_block[i] & 0xFF for i in range(k))) & 0xFFFFFFFF
    ln_digits = _int_to_digits(line_number, ln_width, header_alpha.size)
    crc_digits = _int_to_digits(crc, crc_width, header_alpha.size)

    prefix_text = header_alpha.symbols_to_text(ln_digits + crc_digits)
    payload_text = payload_alpha.symbols_to_text(codeword)
    return prefix_text + payload_text


class LineStatus:
    OK = "ok"
    CORRECTED_SUSPECT = "fragwuerdig_korrigiert"  # RS "loeste" was, aber CRC passt danach nicht
    UNCORRECTABLE = "verworfen"


def parse_payload_line(raw: str, k: int, r: int, gen, gf: GaloisField, ln_width: int, crc_width: int,
                         payload_alpha: Alphabet, header_alpha: Alphabet):
    """Rueckgabe: dict mit line_number, status, data (oder None), reason und
    repaired (None oder Beschreibung einer Laengenreparatur, siehe
    _repair_shifted_payload)."""
    raw = raw.strip()
    if not raw:
        return None  # komplett leere Zeile -> vom Aufrufer wie eine Luecke zu behandeln

    prefix_len = ln_width + crc_width
    prefix_raw, payload_raw = raw[:prefix_len], raw[prefix_len:]

    try:
        prefix_symbols = header_alpha.text_to_symbols(prefix_raw)
    except ValueError:
        return dict(line_number=None, status=LineStatus.UNCORRECTABLE, data=None,
                    reason="Praefix nicht im Header-Alphabet lesbar", repaired=None)

    if len(prefix_symbols) != prefix_len:
        return dict(line_number=None, status=LineStatus.UNCORRECTABLE, data=None,
                    reason="Zeile zu kurz fuer Praefix", repaired=None)

    line_number = _digits_to_int(prefix_symbols[:ln_width], header_alpha.size)
    expected_crc = _digits_to_int(prefix_symbols[ln_width:], header_alpha.size)

    try:
        payload_symbols = payload_alpha.text_to_symbols(payload_raw)
    except ValueError:
        return dict(line_number=line_number, status=LineStatus.UNCORRECTABLE, data=None,
                    reason="Nutzdatenteil nicht im Nutzdaten-Alphabet lesbar", repaired=None)

    if len(payload_symbols) == k + r:
        status, decoded, reason = _decode_codeword(payload_symbols, k, r, gf, expected_crc)
        if status == LineStatus.OK:
            return dict(line_number=line_number, status=status, data=decoded, reason=None, repaired=None)
    else:
        status, decoded, reason = (LineStatus.UNCORRECTABLE, None,
                                   f"Erwartet {k + r} Symbole, {len(payload_symbols)} vorhanden")

    repair = _repair_shifted_payload(payload_raw, k, r, gf, expected_crc, payload_alpha)
    if repair is not None:
        decoded, description = repair
        return dict(line_number=line_number, status=LineStatus.OK, data=decoded, reason=None,
                    repaired=description)
    return dict(line_number=line_number, status=status, data=decoded, reason=reason, repaired=None)


def _crc_of(decoded, k: int) -> int:
    return zlib.crc32(bytes(decoded[i] & 0xFF for i in range(k))) & 0xFFFFFFFF


def _decode_codeword(payload_symbols, k: int, r: int, gf: GaloisField, expected_crc: int):
    """RS-Korrektur + CRC-Pruefung eines Codeworts der Laenge k+r.
    Rueckgabe: (status, daten oder None, grund oder None)."""
    decoded, num_errors = rs_decode(payload_symbols, r, gf)
    if decoded is None:
        return LineStatus.UNCORRECTABLE, None, "Reed-Solomon: zu viele Fehler in dieser Zeile"
    if _crc_of(decoded, k) != expected_crc:
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


def _repair_shifted_payload(payload_raw: str, k: int, r: int, gf: GaloisField, expected_crc: int,
                            payload_alpha: Alphabet):
    """Versucht eine Zeile zu retten, in der die OCR eine Folge gleicher
    Zeichen falsch gezaehlt hat: Ein einziges fehlendes/zusaetzliches Zeichen
    verschiebt den Rest des Codeworts, sodass RS weit mehr als floor(r/2)
    Fehler sieht. Probiert die Varianten aus _shift_candidates durch und
    akzeptiert nur eine, die RS dekodieren kann UND deren CRC32 stimmt - bei
    der Vielzahl an Kandidaten ist der CRC die eigentliche Absicherung gegen
    Falsch-Positive. Bei mehreren Treffern gewinnt der mit den wenigsten
    RS-Korrekturen. Rueckgabe: (daten, beschreibung) oder None."""
    delta = len(payload_raw) - (k + r)
    if abs(delta) > MAX_SHIFT_REPAIR:
        return None
    best = None
    seen = set()
    for candidate, ops in _shift_candidates(payload_raw, delta):
        if candidate in seen:
            continue
        seen.add(candidate)
        decoded, num_errors = rs_decode(payload_alpha.text_to_symbols(candidate), r, gf)
        if decoded is None or _crc_of(decoded, k) != expected_crc:
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
    rows = math.ceil(len(lines) / columns)
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
        cells = [_COLUMN_SPLIT_RE.split(l) for l in block]
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
