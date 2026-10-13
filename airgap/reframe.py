"""
reframe.py: Nimmt base64-codierten Text von stdin und wendet das Air-Gap-
Transfer-Framing (Header, Zeilennummerierung, CRC32, Reed-Solomon) darauf an.

Zwei Annahmen ueber den Input, keine weiteren:
  1. Der Input ist Base64-codiert (Standard-Alphabet A-Z/a-z/0-9/+/).
  2. Zeilenenden sind Unix-LF; sie werden beim Einlesen entfernt.

'=' (Base64-Padding) ist nur ganz am Ende der Eingabe erlaubt und wird
stillschweigend verworfen; ein '=' mitten im Text (z.B. mehrere
aneinandergehaengte Base64-Bloecke) fuehrt zum Abbruch, weil der gemergte
Text sonst kein gueltiges Base64 mehr waere.

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
    base64 geheim.bin | python3 bin/reframe.py --width 100 -o geheim_framed.txt
    python3 bin/decode.py geheim_framed.txt -o geheim.b64
    base64 -d geheim.b64 > geheim.bin   # optionaler Schritt ausserhalb
"""
import argparse
import sys

from .alphabets import DEFAULT_ALPHABET, Alphabet
from .encoder import encode_document, plan_layout, write_page
from .header import TEXT_ENCODING_UTF8

# Erlaubte Zeichen im Base64-Input (ohne '=', das als Padding behandelt wird)
BASE64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789+/"
)


class InputError(ValueError):
    """Eingabe ist kein gueltiger Base64-Text."""


def read_and_merge(raw: bytes) -> bytes:
    """Merged Base64-Zeilen (LF/CRLF entfernen), verwirft das abschliessende
    '='-Padding, validiert das Alphabet und gibt die resultierenden
    ASCII-Bytes zurueck. Wirft InputError bei ungueltigen Zeichen oder
    '=' vor dem Ende der Eingabe."""
    chars = []
    padding_at = None  # (Zeile, Spalte) des ersten '='
    for lineno, line in enumerate(raw.split(b"\n"), 1):
        for col, byte in enumerate(line.rstrip(b"\r"), 1):
            ch = chr(byte)
            if ch == "=":
                padding_at = padding_at or (lineno, col)
                continue
            if ch not in BASE64_CHARS:
                raise InputError(
                    f"Zeichen {ch!r} in Zeile {lineno}, Spalte {col} ist kein gueltiges "
                    f"Base64-Zeichen und kein Padding."
                )
            if padding_at is not None:
                raise InputError(
                    f"'=' in Zeile {padding_at[0]}, Spalte {padding_at[1]} steht nicht am Ende "
                    f"der Eingabe (Padding mitten im Base64-Text)."
                )
            chars.append(ch)
    return "".join(chars).encode("ascii")


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Air-Gap Reframer: Base64-Text von stdin -> geframter Transfer-Text "
            "mit Header, Zeilennummerierung, CRC32 und Reed-Solomon-Paritaet."
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
    # Binaer lesen statt ueber einen ASCII-Textstrom: ein Nicht-ASCII-Byte soll
    # zur selben verstaendlichen Fehlermeldung fuehren wie jedes andere
    # ungueltige Zeichen, nicht zu einem UnicodeDecodeError-Traceback.
    try:
        data = read_and_merge(sys.stdin.buffer.read())  # ASCII-Bytes des gemergten Base64-Texts
    except InputError as e:
        print(f"FEHLER: {e} Abbruch.", file=sys.stderr)
        return 1
    if not data:
        print("FEHLER: Input ist leer (keine Base64-Zeichen gefunden).", file=sys.stderr)
        return 1

    # --- Layout ----------------------------------------------------------------
    # Kein --alphabet-Parameter: DEFAULT_ALPHABET (64 Zeichen, GF(64)) ist fix,
    # weil die Alphabetwahl fuer den Aufrufer irrelevant ist - er sieht nur
    # Base64-Text rein und Base64-Text raus. Jedes Base64-Zeichen wird als
    # ASCII-Byte bit-geslicet (identisch zu encode.py auf einer Textdatei);
    # total_file_size = len(data) erlaubt decode.py, exakt diese Byte-Anzahl
    # zurueckzuliefern = den Original-Base64-Text ohne Padding.
    header_alpha = Alphabet(DEFAULT_ALPHABET)
    payload_alpha = Alphabet(DEFAULT_ALPHABET)
    try:
        layout = plan_layout(data, args.width, payload_alpha, header_alpha, args.redundancy, args.parity_symbols)
    except ValueError as e:
        ap.error(str(e))

    print(
        f"Input: {len(data)} Base64-Zeichen. Layout: Zeilenbreite {layout.effective_width}, "
        f"je Zeile {layout.interleave} Codewort/Codewoerter mit k={layout.k}, r={layout.r} "
        f"(korrigiert bis zu {layout.r // 2} Fehler je Codewort), {layout.total_lines} Nutzdatenzeilen.",
        file=sys.stderr,
    )

    # reframe.py kennt keine Seitenteilung und keine Verwuerfelung (Base64-Text
    # enthaelt praktisch keine langen Folgen gleicher Zeichen) - degenerierter
    # Einzelseiten-Fall, gleicher Code-Pfad wie encode.py ohne --lines.
    (page,) = encode_document(
        data, layout, payload_alpha, header_alpha, preset="default",
        text_encoding=TEXT_ENCODING_UTF8, filename=args.filename, scrambled=False,
        progress=args.progress, progress_label="Reframing",
    )

    # --- Ausgabe ---------------------------------------------------------------
    if args.output:
        with open(args.output, "w", encoding="ascii", newline="\n") as out:
            write_page(out, page)
    else:
        sys.stdout.reconfigure(encoding="ascii", newline="\n")
        write_page(sys.stdout, page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
