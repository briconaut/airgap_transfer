#!/usr/bin/env python3
"""
Encoder: Datei -> Zeilenbasierter Text zur Anzeige in einem reinen Text-Terminal.

Beispiel:
    python3 encode.py geheim.bin -o geheim.txt --width 100 --redundancy 20
    cat geheim.bin | python3 encode.py --width 100 > geheim.txt

Wichtiger Hinweis zur Fehlerkorrektur (siehe README.md):
    r Paritaetssymbole pro Zeile korrigieren NICHT r Fehler, sondern nur
    floor(r/2) Fehler pro Zeile (Reed-Solomon ohne bekannte Erasure-Positionen).
"""
import argparse
import hashlib
import math
import os
import sys
import zlib

# lib/ relativ zu diesem Skript einbinden, unabhaengig vom Aufrufverzeichnis
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from alphabets import Alphabet, DEFAULT_ALPHABET, make_alphabet, PRESET_NAMES
from header import pack_header, build_header_lines, TEXT_ENCODING_UTF8, TEXT_ENCODING_UTF16LE
from rs_codec import GaloisField, rs_generator_poly, rs_encode
from framing import compute_layout, build_payload_line, crc_width_for, byte_offset_of_line
from progress import ProgressBar


def main():
    ap = argparse.ArgumentParser(description="Air-Gap Text-Encoder mit Reed-Solomon-Fehlerkorrektur")
    ap.add_argument("input", nargs="?", help="Eingabedatei (Default: stdin)")
    ap.add_argument(
        "-o", "--output",
        help=(
            "Ausgabedatei (Default: stdout). Enthaelt der Dateiname ein '*', wird es "
            "als Platzhalter fuer eine (optimal, ohne unnoetige Stellen) mit Nullen "
            "aufgefuellte Seitennummer (1-basiert) verwendet und pro Seite eine eigene "
            "Datei geschrieben (mit --lines i.d.R. mehrere, sonst genau eine), statt "
            "einem einzigen Textstrom mit allen Seiten hintereinander."
        ),
    )
    ap.add_argument("--width", type=int, default=100, help="Gesamte Zeilenbreite inkl. aller Felder (Default: 100)")
    alpha_group = ap.add_mutually_exclusive_group()
    alpha_group.add_argument(
        "--preset", choices=PRESET_NAMES, default="default",
        help=(
            "Eingebautes Alphabet-Preset. "
            "'default': 64 Zeichen, konfusionsarm, reines 7-Bit-ASCII (GF64). "
            "'ascii64': 64 Zeichen, alle Buchstaben+Ziffern inkl. bisher ausgeschlossener "
            "Verwechslungspaare, reines 7-Bit-ASCII (GF64, gleiche Dichte wie 'default'). "
            "'latin128': 128 Zeichen, alle druckbaren 7-Bit-ASCII + 34 Latin-1-Zeichen "
            "(GF128, ca. 17%% mehr Nutzdaten/Zeile). Erfordert UTF-8-Terminal und OCR. "
            "'utf8128': wie 'latin128', aber die 34 zusaetzlichen Zeichen kommen aus "
            "Latin Extended-A (mittel-/osteuropaeische Diakritika) statt Latin-1 "
            "(GF128, gleiche Dichte wie 'latin128'). Erfordert UTF-8-Terminal und OCR. "
            "'utf16le128': exakt dieselben 128 Zeichen wie 'utf8128', aber die "
            "AUSGABEDATEI wird als UTF-16LE statt UTF-8 geschrieben (mit BOM) - "
            "fuer verlustfreien Transfer ueber die Windows-Zwischenablage (deren "
            "natives Textformat selbst UTF-16LE ist). "
            "'utf16le256': wie 'utf16le128', aber 256 Zeichen (GF256, 8 Bit/Symbol, "
            "ca. 14%% dichter) - Box-Drawing/Block-Element-Symbole statt OCR-kuratierter "
            "Buchstaben, da fuer Zwischenablage-Transfer (nicht OCR) keine visuelle "
            "Verwechslungsfreiheit noetig ist. decode.py erkennt UTF-16LE automatisch."
        ),
    )
    alpha_group.add_argument(
        "--alphabet",
        help="Custom-Alphabet als String (Laenge muss 32/64/128/256 sein). Schliesst --preset aus.",
    )
    ap.add_argument("--redundancy", type=float, default=None, help="Paritaet in Prozent von k (Default: 20.0)")
    ap.add_argument("--parity-symbols", type=int, default=None, help="Absolute Anzahl Paritaetssymbole r (statt --redundancy)")
    ap.add_argument(
        "--lines", type=int, default=None,
        help=(
            "Nutzdatenzeilen pro Seite. Jede Seite bekommt ihren eigenen, vollstaendigen "
            "Header (inkl. Praeambel, 3-fach wiederholt) sowie Seitenzahl/-nummer, eine "
            "Pruefsumme dieser Seite und eine gemeinsame Dokument-ID. Ohne '*' im "
            "Dateinamen (siehe -o/--output) werden alle Seiten im selben Textstrom "
            "hintereinander ausgegeben, getrennt durch 3 Leerzeilen. "
            "Default: keine Seitenteilung (eine einzige Seite fuer die gesamte Datei)."
        ),
    )
    ap.add_argument("--filename", help="Im Header gespeicherter Dateiname (Default: Basisname der Eingabedatei, falls vorhanden)")
    ap.add_argument("--progress", action="store_true", help="Fortschrittsbalken auf stderr anzeigen")
    args = ap.parse_args()

    if args.redundancy is not None and args.parity_symbols is not None:
        ap.error("--redundancy und --parity-symbols schliessen sich gegenseitig aus")
    if args.lines is not None and args.lines < 1:
        ap.error("--lines muss >= 1 sein")

    # --- Eingabe lesen -----------------------------------------------------
    if args.input:
        with open(args.input, "rb") as f:
            data = f.read()
        default_name = os.path.basename(args.input)
    else:
        data = sys.stdin.buffer.read()
        default_name = ""
    filename = args.filename if args.filename is not None else default_name

    header_alpha = Alphabet(DEFAULT_ALPHABET)
    payload_alpha, is_custom, is_utf16le = make_alphabet(
        preset=args.preset, custom_chars=args.alphabet
    )
    text_encoding = TEXT_ENCODING_UTF16LE if is_utf16le else TEXT_ENCODING_UTF8

    gf = GaloisField(payload_alpha.bits_per_symbol)

    data_symbols = payload_alpha.bytes_to_symbols(data)
    total_data_symbols = len(data_symbols)

    layout = compute_layout(
        width=args.width, total_data_symbols=total_data_symbols,
        payload_alpha=payload_alpha, header_alpha=header_alpha,
        redundancy_pct=args.redundancy, parity_symbols=args.parity_symbols,
    )
    k, r = layout["k"], layout["r"]
    if layout["capped"]:
        print(
            f"Hinweis: --width {args.width} konnte wegen der RS-Codewort-Obergrenze "
            f"({payload_alpha.size - 1} Symbole bei {payload_alpha.size}-Symbol-Alphabet) "
            f"nicht voll ausgenutzt werden. Tatsaechliche Zeilenbreite: {layout['effective_width']}.",
            file=sys.stderr,
        )
    max_correctable = r // 2
    print(
        f"Layout: k={k} Nutzdatensymbole, r={r} Paritaetssymbole "
        f"(korrigiert bis zu {max_correctable} Fehler/Zeile), "
        f"{layout['total_lines']} Nutzdatenzeilen.",
        file=sys.stderr,
    )

    gen = rs_generator_poly(r, gf)

    # --- Nutzdaten in Bloecke aufteilen (letzter Block mit 0 aufgefuellt) --
    total_lines = layout["total_lines"]
    padded_len = total_lines * k
    if len(data_symbols) < padded_len:
        data_symbols = data_symbols + [0] * (padded_len - len(data_symbols))

    payload_lines = []
    bar = ProgressBar(total=total_lines, enabled=args.progress, label="Encoding")
    for i in range(total_lines):
        block = data_symbols[i * k:(i + 1) * k]
        payload_lines.append(
            build_payload_line(i, block, k, r, gen, gf, layout["ln_width"], layout["crc_width"], payload_alpha, header_alpha)
        )
        bar.update(i + 1)
    bar.done()

    # --- Seiten aufteilen und Header bauen -----------------------------------
    sha256 = hashlib.sha256(data).digest()
    bits_per_symbol = payload_alpha.bits_per_symbol
    lines_per_page = args.lines or total_lines
    page_count = max(1, math.ceil(total_lines / lines_per_page))
    document_id = os.urandom(8)

    if page_count > 1:
        print(f"Seitenteilung: {page_count} Seiten zu je bis zu {lines_per_page} Nutzdatenzeilen.", file=sys.stderr)

    pages = []  # (preamble, header_lines, payload_lines_slice)
    for page_number in range(page_count):
        start = page_number * lines_per_page
        end = min(start + lines_per_page, total_lines)
        byte_start = byte_offset_of_line(start, k, bits_per_symbol)
        byte_end = min(byte_offset_of_line(end, k, bits_per_symbol), len(data))
        page_checksum = zlib.crc32(data[byte_start:byte_end]) & 0xFFFFFFFF

        header_bytes = pack_header(
            payload_alpha, is_custom, k, r, len(data), sha256, layout["ln_width"], filename,
            document_id=document_id, page_count=page_count, page_number=page_number,
            page_checksum=page_checksum, text_encoding=text_encoding,
        )
        preamble, header_lines = build_header_lines(header_bytes, args.width, header_alpha)
        pages.append((preamble, header_lines, payload_lines[start:end]))

    # --- Ausgabe -------------------------------------------------------------
    # "utf-16-le" (Python-Codec) fuegt KEIN BOM automatisch an (anders als das
    # generische "utf-16") - fuer ein deterministisches Little-Endian-BOM wird
    # das BOM-Zeichen U+FEFF explizit als allererstes Zeichen geschrieben.
    # decode.py braucht das BOM nicht zwingend (Byte-Sniffing reicht, siehe
    # dort), es ist reine Konvention fuer andere Werkzeuge (Notepad etc.).
    out_encoding = "utf-16-le" if is_utf16le else "utf-8"

    def write_page(out, preamble, header_lines, page_payload_lines, write_bom):
        if write_bom:
            out.write(chr(0xFEFF))  # BOM; chr() statt Literal, um jedes Transkriptionsrisiko auszuschliessen
        out.write(preamble + "\n")
        for line in header_lines:
            out.write(line + "\n")
        for line in page_payload_lines:
            out.write(line + "\n")

    if args.output and "*" in args.output:
        # Pro Seite eine eigene Datei ('*' -> 1-basierte, optimal Null-
        # aufgefuellte Seitennummer). Jede Datei ist ein eigenstaendiger
        # Strom -> bekommt (falls utf16le) IHR EIGENES BOM; keine
        # Leerzeilen-Trenner noetig (die Dateien sind schon physisch getrennt).
        width = len(str(page_count))
        for page_number, (preamble, header_lines, page_payload_lines) in enumerate(pages):
            path = args.output.replace("*", str(page_number + 1).zfill(width))
            with open(path, "w", encoding=out_encoding, newline="\n") as out:
                write_page(out, preamble, header_lines, page_payload_lines, write_bom=is_utf16le)
        print(f"Ausgabe: {page_count} Datei(en), Muster {args.output!r}.", file=sys.stderr)
    else:
        # Ein einziger Textstrom (Datei oder stdout) mit allen Seiten
        # hintereinander -> genau EIN BOM ganz am Anfang, nicht pro Seite.
        if args.output:
            out = open(args.output, "w", encoding=out_encoding, newline="\n")
        else:
            sys.stdout.reconfigure(encoding=out_encoding, newline="\n")
            out = sys.stdout
        try:
            for page_number, (preamble, header_lines, page_payload_lines) in enumerate(pages):
                write_page(out, preamble, header_lines, page_payload_lines, write_bom=(is_utf16le and page_number == 0))
                if page_number < page_count - 1:
                    out.write("\n\n\n")  # 3 Leerzeilen als Seitentrenner, vom Decoder ignoriert
        finally:
            if args.output:
                out.close()


if __name__ == "__main__":
    main()
