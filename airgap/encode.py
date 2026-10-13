"""
Encoder: Datei -> Zeilenbasierter Text zur Anzeige in einem reinen Text-Terminal.

Beispiel:
    python3 bin/encode.py geheim.bin -o geheim.txt --width 100 --redundancy 20
    cat geheim.bin | python3 bin/encode.py --width 100 > geheim.txt
    python3 bin/encode.py geheim.bin -o geheim.txt --format-version 4   # fuer aelteren decode.py

Wichtiger Hinweis zur Fehlerkorrektur (siehe README.md):
    r Paritaetssymbole pro Zeile korrigieren NICHT r Fehler, sondern nur
    floor(r/2) Fehler pro Zeile (Reed-Solomon ohne bekannte Erasure-Positionen).
"""
import argparse
import os
import sys

from .alphabets import DEFAULT_ALPHABET, ENCODING_UTF8, PRESET_NAMES, PRESETS, Alphabet, make_alphabet
from .encode_options import DEFAULT_WIDTH, ENCODE_OPTIONS
from .encoder import PAGE_SEPARATOR, encode_document, plan_layout, write_page
from .formats import CURRENT_FORMAT_VERSION, SUPPORTED_FORMAT_VERSIONS
from .header import CODEC_TO_TEXT_ENCODING, MAX_PAGE_COUNT
from .util import ceil_div


def _add_tool_arguments(ap: argparse.ArgumentParser):
    """Optionen ohne Einfluss auf das Dateiformat (fuer alle Formatversionen)."""
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
    ap.add_argument("--filename", help="Im Header gespeicherter Dateiname (Default: Basisname der Eingabedatei, falls vorhanden)")
    ap.add_argument("--progress", action="store_true", help="Fortschrittsbalken auf stderr anzeigen")
    ap.add_argument(
        "--show-preset", choices=PRESET_NAMES, metavar="PRESET",
        help=(
            "Gibt alle Zeichen, die eine mit diesem Preset kodierte Datei enthalten kann "
            "(Header-Alphabet + Nutzdaten-Alphabet), als eine Zeile UTF-8 auf stdout aus "
            "- z.B. fuer Tesseracts tessedit_char_whitelist - und beendet sich ohne zu kodieren."
        ),
    )


def _add_format_version_argument(ap: argparse.ArgumentParser):
    ap.add_argument(
        "--format-version", type=int, choices=SUPPORTED_FORMAT_VERSIONS, default=CURRENT_FORMAT_VERSION,
        help=(
            f"Formatversion der Ausgabe (Default: {CURRENT_FORMAT_VERSION}), fuer Empfaenger mit "
            f"aelterem decode.py. Bestimmt auch die verfuegbaren Optionen - "
            f"'--format-version N --help' zeigt sie."
        ),
    )


def main():
    description = "Air-Gap Text-Encoder mit Reed-Solomon-Fehlerkorrektur"
    # Phase 1: nur --format-version, davon haengen die uebrigen Optionen ab
    pre = argparse.ArgumentParser(description=description, add_help=False)
    _add_format_version_argument(pre)
    known, _ = pre.parse_known_args()
    opts = ENCODE_OPTIONS[known.format_version]
    fmt = opts.format

    # Phase 2: vollstaendiger Parser mit den Optionen dieser Version
    ap = argparse.ArgumentParser(description=description)
    _add_tool_arguments(ap)
    _add_format_version_argument(ap)
    opts.add_arguments(ap)
    args = ap.parse_args()

    if args.show_preset:
        # Praeambel, Header, Zeilennummern und CRC nutzen immer das Header-
        # Alphabet (inkl. '#' des Spaltentrenners), die Nutzdaten das Preset.
        chars = dict.fromkeys(DEFAULT_ALPHABET + PRESETS[args.show_preset].chars)
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        print("".join(chars))
        return 0

    opts.validate(ap, args)
    columns = opts.columns(args)

    # --- Eingabe lesen -----------------------------------------------------
    if args.input:
        try:
            with open(args.input, "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"FEHLER: Eingabedatei nicht lesbar ({e}).", file=sys.stderr)
            return 1
        default_name = os.path.basename(args.input)
    else:
        data = sys.stdin.buffer.read()
        default_name = ""
    filename = args.filename if args.filename is not None else default_name

    header_alpha = Alphabet(DEFAULT_ALPHABET)
    try:
        payload_alpha, preset, out_codec = make_alphabet(preset=args.preset, custom_chars=args.alphabet)
        # Default-Breite darf auf das Maximum der Formatversion gekuerzt
        # werden, eine explizit angegebene nicht (dann Fehler)
        layout = plan_layout(data, args.width or DEFAULT_WIDTH, payload_alpha, header_alpha,
                             args.redundancy, args.parity_symbols, fmt=fmt, clamp_width=args.width is None)
    except ValueError as e:
        ap.error(str(e))

    if layout.width_clamped:
        print(
            f"Hinweis: Default-Breite {DEFAULT_WIDTH} ist fuer Formatversion {fmt.version} mit diesem "
            f"Alphabet zu gross, verwende {layout.effective_width}.",
            file=sys.stderr,
        )
    print(
        f"Formatversion {fmt.version}, Layout: Zeilenbreite {layout.effective_width}, je Zeile {layout.interleave} verschraenkte(s) "
        f"Codewort/Codewoerter mit k={layout.k} Nutzdaten- und r={layout.r} Paritaetssymbolen "
        f"(korrigiert bis zu {layout.r // 2} Fehler je Codewort), {layout.total_lines} Nutzdatenzeilen.",
        file=sys.stderr,
    )

    # --lines zaehlt die Zeilen nach dem Aufteilen in Spalten -> eine Seite
    # fasst --lines x --columns Nutzdatenzeilen.
    lines_per_page = args.lines * columns if args.lines else None
    page_count = ceil_div(layout.total_lines, lines_per_page) if lines_per_page else 1
    if page_count > MAX_PAGE_COUNT:
        ap.error(f"{page_count} Seiten waeren noetig, maximal {MAX_PAGE_COUNT} moeglich - --lines vergroessern")
    if page_count > 1:
        print(f"Seitenteilung: {page_count} Seiten zu je bis zu {lines_per_page} Nutzdatenzeilen.", file=sys.stderr)

    pages = encode_document(
        data, layout, payload_alpha, header_alpha, preset,
        text_encoding=CODEC_TO_TEXT_ENCODING[out_codec], filename=filename,
        scrambled=opts.scrambled(args), lines_per_page=lines_per_page, columns=columns,
        progress=args.progress, fmt=fmt,
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
    # etwas schmaler als --width sein, siehe Layout.effective_width).
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
                write_page(out, page, columns, col_width, write_bom=needs_bom)
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
                write_page(out, page, columns, col_width, write_bom=needs_bom and page_number == 0)
        finally:
            if args.output:
                out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
