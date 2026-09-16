#!/usr/bin/env python3
"""
reframe.py: Nimmt base64-codierten Text von stdin und wendet das Air-Gap-
Transfer-Framing (Header, Zeilennummerierung, CRC32, Reed-Solomon) darauf an.

Zwei Annahmen ueber den Input, keine weiteren:
  1. Der Input ist Base64-codiert (Standard-Alphabet A-Z/a-z/0-9/+/).
  2. Zeilenenden sind Unix-LF; sie werden beim Einlesen entfernt.

Trailing '=' (Base64-Padding) wird stillschweigend verworfen.

Die Ausgabe ist direkt mit decode.py kompatibel. decode.py reproduziert dabei
exakt den gemergten Base64-Text (ohne '=') als Bytes — identisch zum Input
dieses Tools.

Implementierungsnotiz:
    Jedes Base64-Zeichen wird als sein ASCII-Byte-Wert behandelt und dann
    normal bit-geslicet (identisch zu encode.py auf einer Textdatei). So
    kennt decode.py die exakte Byte-Anzahl (total_file_size = Zeichenanzahl)
    und gibt den Base64-Text unveraendert zurueck — ohne weiteren Decode-
    Schritt. Alternativansatz (jedes Zeichen direkt als 6-Bit-Symbol) wurde
    verworfen, weil bit-slicing dann die Zeichen zerstoert statt erholt.

Beispiel:
    base64 geheim.bin | python3 reframe.py --width 100 -o geheim_framed.txt
    python3 decode.py geheim_framed.txt -o geheim.b64
    base64 -d geheim.b64 > geheim.bin   # optionaler Schritt ausserhalb
"""
import argparse
import hashlib
import os
import sys

# lib/ relativ zu diesem Skript einbinden, unabhaengig vom Aufrufverzeichnis
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from alphabets import Alphabet, DEFAULT_ALPHABET
from header import pack_header, build_header_lines
from rs_codec import GaloisField, rs_generator_poly
from framing import compute_layout, build_payload_line
from progress import ProgressBar

# Erlaubte Zeichen im Base64-Input (ohne '=', das als Padding behandelt wird)
BASE64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789+/"
)


def read_and_merge(stream) -> bytes:
    """Liest Base64-Text aus stream, merged Zeilen (LF entfernen), strippt '=',
    validiert das Alphabet, gibt die resultierenden ASCII-Bytes zurueck.

    Zeichen ausserhalb des Base64-Alphabets (ausser '=' und LF) werden als
    Fehler behandelt — die einzige erlaubte Annahme ist Base64-Input.
    """
    chars = []
    for lineno, line in enumerate(stream, 1):
        line = line.rstrip("\n").rstrip("\r")
        for col, ch in enumerate(line, 1):
            if ch == "=":
                continue  # Padding: verwerfen
            if ch not in BASE64_CHARS:
                print(
                    f"FEHLER: Zeichen {ch!r} in Zeile {lineno}, Spalte {col} "
                    f"ist kein gueltiges Base64-Zeichen und kein Padding — Abbruch.",
                    file=sys.stderr,
                )
                sys.exit(1)
            chars.append(ch)
    return "".join(chars).encode("ascii")


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Air-Gap Reframer: Base64-Text von stdin -> geframter Transfer-Text "
            "mit Header, Zeilennummerierung, CRC32 und Reed-Solomon-Parität."
        )
    )
    ap.add_argument("-o", "--output", help="Ausgabedatei (Default: stdout)")
    ap.add_argument(
        "--width", type=int, default=100,
        help="Gesamte Zeilenbreite inkl. aller Felder (Default: 100)",
    )
    ap.add_argument(
        "--redundancy", type=float, default=None,
        help="Paritaet in Prozent von k (Default: 20.0)",
    )
    ap.add_argument(
        "--parity-symbols", type=int, default=None,
        help="Absolute Anzahl Paritaetssymbole r (statt --redundancy)",
    )
    ap.add_argument(
        "--filename", default="",
        help="Im Header gespeicherter Dateiname (Default: leer)",
    )
    ap.add_argument("--progress", action="store_true", help="Fortschrittsbalken auf stderr anzeigen")
    args = ap.parse_args()

    if args.redundancy is not None and args.parity_symbols is not None:
        ap.error("--redundancy und --parity-symbols schliessen sich gegenseitig aus")

    # --- Input lesen und mergen -----------------------------------------------
    sys.stdin.reconfigure(encoding="ascii", errors="strict")
    data = read_and_merge(sys.stdin)  # ASCII-Bytes des gemergten Base64-Texts

    if not data:
        print("FEHLER: Input ist leer (keine Base64-Zeichen gefunden).", file=sys.stderr)
        sys.exit(1)

    # --- Alphabete und GF einrichten ------------------------------------------
    # Kein --alphabet-Parameter: DEFAULT_ALPHABET (64 Zeichen, GF(64)) ist fix,
    # weil die Alphabetwahl fuer den Aufrufer irrelevant ist — er sieht nur
    # Base64-Text rein und Base64-Text raus.
    header_alpha = Alphabet(DEFAULT_ALPHABET)
    payload_alpha = Alphabet(DEFAULT_ALPHABET)
    gf = GaloisField(payload_alpha.bits_per_symbol)  # GF(2^6) = GF(64)

    # --- Symbole aus den ASCII-Bytes extrahieren --------------------------------
    # Jedes ASCII-Byte wird bit-geslicet (identisch zu encode.py).
    # total_file_size = len(data) erlaubt decode.py, exakt diese Byte-Anzahl
    # zurueckzuliefern = den Original-Base64-Text ohne Padding.
    data_symbols = payload_alpha.bytes_to_symbols(data)
    total_data_symbols = len(data_symbols)

    # --- Layout berechnen ------------------------------------------------------
    layout = compute_layout(
        width=args.width, total_data_symbols=total_data_symbols,
        payload_alpha=payload_alpha, header_alpha=header_alpha,
        redundancy_pct=args.redundancy, parity_symbols=args.parity_symbols,
    )
    k, r = layout["k"], layout["r"]

    if layout["capped"]:
        print(
            f"Hinweis: --width {args.width} konnte wegen der RS-Codewort-Obergrenze "
            f"({payload_alpha.size - 1} Symbole) nicht voll ausgenutzt werden. "
            f"Tatsaechliche Zeilenbreite: {layout['effective_width']}.",
            file=sys.stderr,
        )

    print(
        f"Input: {len(data)} Base64-Zeichen, {total_data_symbols} Symbols. "
        f"Layout: k={k}, r={r} (korrigiert bis zu {r // 2} Fehler/Zeile), "
        f"{layout['total_lines']} Nutzdatenzeilen.",
        file=sys.stderr,
    )

    gen = rs_generator_poly(r, gf)

    # --- Symbole in Bloecke aufteilen ------------------------------------------
    total_lines = layout["total_lines"]
    padded_len = total_lines * k
    if len(data_symbols) < padded_len:
        data_symbols = data_symbols + [0] * (padded_len - len(data_symbols))

    payload_lines = []
    bar = ProgressBar(total=total_lines, enabled=args.progress, label="Reframing")
    for i in range(total_lines):
        block = data_symbols[i * k:(i + 1) * k]
        payload_lines.append(
            build_payload_line(
                i, block, k, r, gen, gf,
                layout["ln_width"], layout["crc_width"],
                payload_alpha, header_alpha,
            )
        )
        bar.update(i + 1)
    bar.done()

    # --- Header bauen ----------------------------------------------------------
    # SHA-256 des gemergten Base64-Texts (ohne '=') — ermoeglicht decode.py
    # die Integritaetspruefung der uebertragenen Base64-Daten.
    sha256_digest = hashlib.sha256(data).digest()

    header_bytes = pack_header(
        payload_alpha,
        is_custom_alphabet=False,       # DEFAULT_ALPHABET, kein Custom
        k=k, r=r,
        total_file_size=len(data),      # Byte-Anzahl = Zeichenanzahl (ASCII)
        sha256_digest=sha256_digest,
        ln_width=layout["ln_width"],
        filename=args.filename,
    )
    preamble, header_lines = build_header_lines(header_bytes, args.width, header_alpha)

    # --- Ausgabe ---------------------------------------------------------------
    if args.output:
        out = open(args.output, "w", encoding="ascii", newline="\n")
    else:
        sys.stdout.reconfigure(encoding="ascii", newline="\n")
        out = sys.stdout
    try:
        out.write(preamble + "\n")
        for line in header_lines:
            out.write(line + "\n")
        for line in payload_lines:
            out.write(line + "\n")
    finally:
        if args.output:
            out.close()


if __name__ == "__main__":
    main()
