"""
Encoder: Datei -> Zeilenbasierter Text zur Anzeige in einem reinen Text-Terminal.

Beispiel:
    python3 bin/encode.py geheim.bin -o geheim.txt --width 100 --redundancy 20
    cat geheim.bin | python3 bin/encode.py --width 100 > geheim.txt

Wichtiger Hinweis zur Fehlerkorrektur (siehe README.md):
    r Paritaetssymbole pro Zeile korrigieren NICHT r Fehler, sondern nur
    floor(r/2) Fehler pro Zeile (Reed-Solomon ohne bekannte Erasure-Positionen).
"""
import argparse
import os
import sys

from .alphabets import DEFAULT_ALPHABET, ENCODING_UTF8, PRESET_NAMES, PRESETS, Alphabet, make_alphabet
from .encoder import PAGE_SEPARATOR, encode_document, plan_layout, write_page
from .header import CODEC_TO_TEXT_ENCODING, MAX_PAGE_COUNT
from .util import ceil_div


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
            "Verwechslungsfreiheit noetig ist. "
            "'utf32le128'/'utf32le256': dieselben Zeichen wie 'utf16le128'/'utf16le256', "
            "aber die AUSGABEDATEI wird als UTF-32LE geschrieben - fuer den Workflow "
            "'Get-Content -Encoding UTF32 datei.txt | Set-Clipboard' in PowerShell. "
            "'utf32le512'/'utf32be512': NEUES 512-Zeichen-Set (GF512, 9 Bit/Symbol, "
            "ca. 13%% dichter als 256er-Presets) aus dem Unicode-Block Mathematical "
            "Alphanumeric Symbols - nur mit UTF-32 nutzbar, da UTF-16LE dafuer "
            "Surrogatpaare braeuchte. 'le'/'be' waehlt die Byte-Reihenfolge der "
            "Ausgabedatei (passend zu PowerShells 'UTF32'/'BigEndianUTF32'-Encodings). "
            "decode.py erkennt UTF-16LE/UTF-32LE/UTF-32BE automatisch."
        ),
    )
    alpha_group.add_argument(
        "--alphabet",
        help="Custom-Alphabet als String (Laenge muss 32/64/128/256/512 sein). Schliesst --preset aus.",
    )
    ap.add_argument("--redundancy", type=float, default=None, help="Paritaet in Prozent von k (Default: 20.0)")
    ap.add_argument("--parity-symbols", type=int, default=None, help="Absolute Anzahl Paritaetssymbole r (statt --redundancy)")
    ap.add_argument(
        "--lines", type=int, default=None,
        help=(
            "Nutzdatenzeilen pro Seite; mit --columns die Zeilen NACH dem Aufteilen "
            "in Spalten (eine Seite fasst dann --lines x --columns Nutzdatenzeilen). Jede Seite bekommt ihren eigenen, vollstaendigen "
            "Header (inkl. Praeambel, 3-fach wiederholt) sowie Seitenzahl/-nummer, eine "
            "Pruefsumme dieser Seite und eine gemeinsame Dokument-ID. Ohne '*' im "
            "Dateinamen (siehe -o/--output) werden alle Seiten im selben Textstrom "
            "hintereinander ausgegeben, getrennt durch 3 Leerzeilen. "
            "Default: keine Seitenteilung (eine einzige Seite fuer die gesamte Datei)."
        ),
    )
    ap.add_argument(
        "--columns", type=int, default=1,
        help=(
            "Anzahl Spalten, auf die die Nutzdatenzeilen jeder Seite verteilt werden "
            "(spaltenweise: erst Spalte 1 von oben nach unten, dann Spalte 2, ...), "
            "getrennt durch ' ### '. Praeambel und Header bleiben einspaltig. "
            "Die Breite jeder Spalte entspricht --width. decode.py erkennt das "
            "Spaltenlayout automatisch. Default: 1"
        ),
    )
    ap.add_argument("--filename", help="Im Header gespeicherter Dateiname (Default: Basisname der Eingabedatei, falls vorhanden)")
    ap.add_argument("--progress", action="store_true", help="Fortschrittsbalken auf stderr anzeigen")
    ap.add_argument(
        "--no-scramble", action="store_true",
        help=(
            "Nutzdaten NICHT verwuerfeln. Standardmaessig werden die Datensymbole "
            "jeder Zeile mit einem aus der Dokument-ID abgeleiteten Pseudo-Zufallsstrom "
            "XOR-verknuepft, damit keine langen Folgen gleicher Zeichen entstehen "
            "(die OCR haeufig falsch zaehlt). Nur fuer Vergleiche/Debugging."
        ),
    )
    ap.add_argument(
        "--show-preset", choices=PRESET_NAMES, metavar="PRESET",
        help=(
            "Gibt alle Zeichen, die eine mit diesem Preset kodierte Datei enthalten kann "
            "(Header-Alphabet + Nutzdaten-Alphabet), als eine Zeile UTF-8 auf stdout aus "
            "- z.B. fuer Tesseracts tessedit_char_whitelist - und beendet sich ohne zu kodieren."
        ),
    )
    args = ap.parse_args()

    if args.show_preset:
        # Praeambel, Header, Zeilennummern und CRC nutzen immer das Header-
        # Alphabet (inkl. '#' des Spaltentrenners), die Nutzdaten das Preset.
        chars = dict.fromkeys(DEFAULT_ALPHABET + PRESETS[args.show_preset].chars)
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        print("".join(chars))
        return 0

    if args.redundancy is not None and args.parity_symbols is not None:
        ap.error("--redundancy und --parity-symbols schliessen sich gegenseitig aus")
    if args.lines is not None and args.lines < 1:
        ap.error("--lines muss >= 1 sein")
    if args.columns < 1:
        ap.error("--columns muss >= 1 sein")

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
    try:
        payload_alpha, is_custom, out_codec = make_alphabet(preset=args.preset, custom_chars=args.alphabet)
        layout = plan_layout(data, args.width, payload_alpha, header_alpha, args.redundancy, args.parity_symbols)
    except ValueError as e:
        ap.error(str(e))

    if layout.capped:
        print(
            f"Hinweis: --width {args.width} konnte wegen der RS-Codewort-Obergrenze "
            f"({payload_alpha.size - 1} Symbole bei {payload_alpha.size}-Symbol-Alphabet) "
            f"nicht voll ausgenutzt werden. Tatsaechliche Zeilenbreite: {layout.effective_width}.",
            file=sys.stderr,
        )
    print(
        f"Layout: k={layout.k} Nutzdatensymbole, r={layout.r} Paritaetssymbole "
        f"(korrigiert bis zu {layout.r // 2} Fehler/Zeile), "
        f"{layout.total_lines} Nutzdatenzeilen.",
        file=sys.stderr,
    )

    # --lines zaehlt die Zeilen nach dem Aufteilen in Spalten -> eine Seite
    # fasst --lines x --columns Nutzdatenzeilen.
    lines_per_page = args.lines * args.columns if args.lines else None
    page_count = ceil_div(layout.total_lines, lines_per_page) if lines_per_page else 1
    if page_count > MAX_PAGE_COUNT:
        ap.error(f"{page_count} Seiten waeren noetig, maximal {MAX_PAGE_COUNT} moeglich - --lines vergroessern")
    if page_count > 1:
        print(f"Seitenteilung: {page_count} Seiten zu je bis zu {lines_per_page} Nutzdatenzeilen.", file=sys.stderr)

    pages = encode_document(
        data, layout, args.width, payload_alpha, header_alpha, is_custom,
        text_encoding=CODEC_TO_TEXT_ENCODING[out_codec], filename=filename,
        scrambled=not args.no_scramble, lines_per_page=lines_per_page, progress=args.progress,
    )

    # --- Ausgabe -------------------------------------------------------------
    # "utf-16-le"/"utf-32-le"/"utf-32-be" (Python-Codecs) fuegen KEIN BOM
    # automatisch an (anders als die generischen "utf-16"/"utf-32") - fuer ein
    # deterministisches BOM wird U+FEFF explizit als allererstes Zeichen
    # geschrieben; der jeweilige Codec kodiert es dann in der richtigen
    # Byte-Reihenfolge/-breite (z.B. FF FE 00 00 bei utf-32-le, 00 00 FE FF bei
    # utf-32-be). decode.py braucht das BOM nicht zwingend (Byte-Sniffing
    # reicht, siehe dort), es ist reine Konvention fuer andere Werkzeuge.
    needs_bom = out_codec != ENCODING_UTF8

    # Einheitliche Spaltenbreite ueber alle Seiten (Nutzdatenzeilen koennen
    # wegen der RS-Obergrenze schmaler als --width sein, siehe "capped").
    col_width = layout.effective_width

    if args.output and "*" in args.output:
        # Pro Seite eine eigene Datei ('*' -> 1-basierte, optimal Null-
        # aufgefuellte Seitennummer). Jede Datei ist ein eigenstaendiger
        # Strom -> bekommt (falls nicht UTF-8) IHR EIGENES BOM; keine
        # Leerzeilen-Trenner noetig (die Dateien sind schon physisch getrennt).
        digits = len(str(len(pages)))
        for page_number, page in enumerate(pages):
            path = args.output.replace("*", str(page_number + 1).zfill(digits))
            with open(path, "w", encoding=out_codec, newline="\n") as out:
                write_page(out, page, args.columns, col_width, write_bom=needs_bom)
        print(f"Ausgabe: {len(pages)} Datei(en), Muster {args.output!r}.", file=sys.stderr)
    else:
        # Ein einziger Textstrom (Datei oder stdout) mit allen Seiten
        # hintereinander -> genau EIN BOM ganz am Anfang, nicht pro Seite.
        if args.output:
            out = open(args.output, "w", encoding=out_codec, newline="\n")
        else:
            sys.stdout.reconfigure(encoding=out_codec, newline="\n")
            out = sys.stdout
        try:
            for page_number, page in enumerate(pages):
                if page_number:
                    out.write(PAGE_SEPARATOR)  # vom Decoder ignoriert
                write_page(out, page, args.columns, col_width, write_bom=needs_bom and page_number == 0)
        finally:
            if args.output:
                out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
