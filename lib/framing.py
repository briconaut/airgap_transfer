"""
Nutzdatenzeile:  [Zeilennummer: ln_width Symbole, Header-Alphabet]
                 [CRC32:        crc_width Symbole, Header-Alphabet]
                 [RS-Codewort:  k+r Symbole, Nutzdaten-Alphabet]  (=Payload+Paritaet)

CRC32 wird NUR ueber die k Nutzdatensymbole berechnet (nicht ueber die
Zeilennummer - bewusste Design-Entscheidung, siehe Grilling-Runde 12).
"""
import math
import zlib
from alphabets import Alphabet
from rs_codec import GaloisField, rs_generator_poly, rs_encode, rs_decode

CRC_BITS = 32


def crc_width_for(header_alpha: Alphabet) -> int:
    return math.ceil(CRC_BITS / header_alpha.bits_per_symbol)


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
    """Rueckgabe: dict mit line_number, status, data (oder None)."""
    raw = raw.strip()
    if not raw:
        return None  # komplett leere Zeile -> vom Aufrufer wie eine Luecke zu behandeln

    prefix_len = ln_width + crc_width
    prefix_raw, payload_raw = raw[:prefix_len], raw[prefix_len:]

    try:
        prefix_symbols = header_alpha.text_to_symbols(prefix_raw)
    except ValueError:
        return dict(line_number=None, status=LineStatus.UNCORRECTABLE, data=None,
                    reason="Praefix nicht im Header-Alphabet lesbar")

    if len(prefix_symbols) != prefix_len:
        return dict(line_number=None, status=LineStatus.UNCORRECTABLE, data=None,
                    reason="Zeile zu kurz fuer Praefix")

    line_number = _digits_to_int(prefix_symbols[:ln_width], header_alpha.size)
    expected_crc = _digits_to_int(prefix_symbols[ln_width:], header_alpha.size)

    try:
        payload_symbols = payload_alpha.text_to_symbols(payload_raw)
    except ValueError:
        return dict(line_number=line_number, status=LineStatus.UNCORRECTABLE, data=None,
                    reason="Nutzdatenteil nicht im Nutzdaten-Alphabet lesbar")

    if len(payload_symbols) != k + r:
        return dict(line_number=line_number, status=LineStatus.UNCORRECTABLE, data=None,
                    reason=f"Erwartet {k + r} Symbole, {len(payload_symbols)} vorhanden")

    decoded, num_errors = rs_decode(payload_symbols, r, gf)
    if decoded is None:
        return dict(line_number=line_number, status=LineStatus.UNCORRECTABLE, data=None,
                    reason="Reed-Solomon: zu viele Fehler in dieser Zeile")

    actual_crc = zlib.crc32(bytes(decoded[i] & 0xFF for i in range(k))) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        return dict(line_number=line_number, status=LineStatus.CORRECTED_SUSPECT, data=decoded,
                    reason=f"RS meldet {num_errors} korrigierte Fehler, aber CRC passt danach nicht")

    return dict(line_number=line_number, status=LineStatus.OK, data=decoded, reason=None)


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
