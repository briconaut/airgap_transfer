"""
Formatrelevante Kommandozeilenoptionen von encode.py je Formatversion.

Jede Klasse bildet den Optionsumfang ab, den encode.py zu Zeiten dieser
Formatversion hatte (z.B. kennt Version 3 weder --columns noch
--no-scramble), und erbt linear von ihrer Vorgaengerin - analog zu den
Formatklassen in formats/. Werkzeugoptionen ohne Einfluss auf das
Dateiformat (Eingabe, -o, --filename, --progress, --show-preset,
--format-version) stehen in encode.py und gelten fuer alle Versionen.
"""
import argparse

from .alphabets import ALLOWED_ALPHABET_SIZES, PRESET_NAMES
from .formats import FORMATS, Format

DEFAULT_WIDTH = 100

# Hilfetexte je Preset; angezeigt werden nur die der gewaehlten Version.
PRESET_HELP = {
    "default": "64 Zeichen, konfusionsarm, reines 7-Bit-ASCII (GF64).",
    "ascii64": (
        "64 Zeichen, alle Buchstaben+Ziffern inkl. bisher ausgeschlossener "
        "Verwechslungspaare, reines 7-Bit-ASCII (GF64, gleiche Dichte wie 'default')."
    ),
    "latin128": (
        "128 Zeichen, alle druckbaren 7-Bit-ASCII + 34 Latin-1-Zeichen "
        "(GF128, ca. 17%% mehr Nutzdaten/Zeile). Erfordert UTF-8-Terminal und OCR."
    ),
    "utf8128": (
        "wie 'latin128', aber die 34 zusaetzlichen Zeichen kommen aus "
        "Latin Extended-A (mittel-/osteuropaeische Diakritika) statt Latin-1 "
        "(GF128, gleiche Dichte wie 'latin128'). Erfordert UTF-8-Terminal und OCR."
    ),
    "utf16le128": (
        "exakt dieselben 128 Zeichen wie 'utf8128', aber die AUSGABEDATEI wird als "
        "UTF-16LE statt UTF-8 geschrieben (mit BOM) - fuer verlustfreien Transfer ueber "
        "die Windows-Zwischenablage (deren natives Textformat selbst UTF-16LE ist)."
    ),
    "utf16le256": (
        "wie 'utf16le128', aber 256 Zeichen (GF256, 8 Bit/Symbol, ca. 14%% dichter) - "
        "Box-Drawing/Block-Element-Symbole statt OCR-kuratierter Buchstaben, da fuer "
        "Zwischenablage-Transfer (nicht OCR) keine visuelle Verwechslungsfreiheit noetig ist."
    ),
    "utf32le128": (
        "dieselben Zeichen wie 'utf16le128', aber die AUSGABEDATEI wird als UTF-32LE "
        "geschrieben - fuer 'Get-Content -Encoding UTF32 datei.txt | Set-Clipboard' in PowerShell."
    ),
    "utf32le256": "wie 'utf32le128', aber mit den Zeichen von 'utf16le256'.",
    "utf32le512": (
        "NEUES 512-Zeichen-Set (GF512, 9 Bit/Symbol, ca. 13%% dichter als 256er-Presets) "
        "aus dem Unicode-Block Mathematical Alphanumeric Symbols - nur mit UTF-32 nutzbar, "
        "da UTF-16LE dafuer Surrogatpaare braeuchte. Ausgabedatei UTF-32LE "
        "(PowerShell-Encoding 'UTF32')."
    ),
    "utf32be512": "wie 'utf32le512', aber Ausgabedatei UTF-32BE (PowerShell-Encoding 'BigEndianUTF32').",
}
assert set(PRESET_HELP) == set(PRESET_NAMES), "jedes Preset braucht einen Hilfetext"


class EncodeOptionsV3:
    """Formatversion 3: --width, --preset/--alphabet, --redundancy,
    --parity-symbols, --lines. Keine Verwuerfelung, keine Spalten, nur die
    Presets bis 256 Zeichen in UTF-8/UTF-16LE (die frueheren v3-Decoder
    kennen weder UTF-32 noch 512er-Alphabete)."""

    version = 3
    presets = ("default", "ascii64", "latin128", "utf8128", "utf16le128", "utf16le256")
    alphabet_sizes = (32, 64, 128, 256)

    @property
    def format(self) -> Format:
        return FORMATS[self.version]

    def add_arguments(self, ap: argparse.ArgumentParser):
        width_help = (
            f"Breite einer Nutzdatenzeile in Zeichen inkl. Zeilennummer und Pruefsumme "
            f"(Default: {DEFAULT_WIDTH})."
        )
        if self.format.max_interleave is None:
            width_help += (
                " Wird unabhaengig vom Alphabet ausgenutzt: passt ein einzelnes RS-Codewort "
                "nicht hinein, werden mehrere verschraenkt."
            )
        else:
            width_help += (
                f" In Formatversion {self.version} passt nur ein RS-Codewort in eine Zeile "
                f"(hoechstens Alphabetgroesse-1 Symbole) - ist der Default dafuer zu breit, "
                f"wird er gekuerzt, ein explizit zu grosser Wert ist ein Fehler."
            )
        ap.add_argument("--width", type=int, default=None, help=self._width_help_suffix(width_help))
        alpha_group = ap.add_mutually_exclusive_group()
        alpha_group.add_argument(
            "--preset", choices=self.presets, default="default",
            help="Eingebautes Alphabet-Preset. " + " ".join(
                f"'{name}': {PRESET_HELP[name]}" for name in self.presets
            ) + " decode.py erkennt das Encoding der Datei automatisch.",
        )
        sizes = "/".join(str(s) for s in self.alphabet_sizes)
        alpha_group.add_argument(
            "--alphabet",
            help=f"Custom-Alphabet als String (Laenge muss {sizes} sein). Schliesst --preset aus.",
        )
        ap.add_argument("--redundancy", type=float, default=None, help="Paritaet in Prozent von k (Default: 20.0)")
        ap.add_argument("--parity-symbols", type=int, default=None,
                        help="Absolute Anzahl Paritaetssymbole r (statt --redundancy)")
        ap.add_argument(
            "--lines", type=int, default=None,
            help=(
                "Nutzdatenzeilen pro Seite" + self._lines_help_columns() + ". Jede Seite bekommt "
                "ihren eigenen, vollstaendigen Header (inkl. Praeambel, 3-fach wiederholt) sowie "
                "Seitenzahl/-nummer, eine Pruefsumme dieser Seite und eine gemeinsame Dokument-ID. "
                "Ohne '*' im Dateinamen (siehe -o/--output) werden alle Seiten im selben Textstrom "
                "hintereinander ausgegeben, getrennt durch 3 Leerzeilen. "
                "Default: keine Seitenteilung (eine einzige Seite fuer die gesamte Datei)."
            ),
        )

    def _width_help_suffix(self, text: str) -> str:
        return text

    def _lines_help_columns(self) -> str:
        return ""

    def validate(self, ap: argparse.ArgumentParser, args):
        """Prueft Optionskombinationen; Fehler ueber ap.error (Exitcode 2)."""
        if args.redundancy is not None and args.parity_symbols is not None:
            ap.error("--redundancy und --parity-symbols schliessen sich gegenseitig aus")
        if args.lines is not None and args.lines < 1:
            ap.error("--lines muss >= 1 sein")
        if args.alphabet is not None and len(args.alphabet) not in self.alphabet_sizes:
            ap.error(
                f"--alphabet: Laenge {len(args.alphabet)} ist in Formatversion {self.version} "
                f"nicht erlaubt (erlaubt: {self.alphabet_sizes})"
            )

    def scrambled(self, args) -> bool:
        return False

    def columns(self, args) -> int:
        return 1


class EncodeOptionsV4(EncodeOptionsV3):
    """Formatversion 4 = 3 + --columns, --no-scramble und alle Presets/
    Alphabetgroessen (UTF-32, 512 Zeichen)."""

    version = 4
    presets = tuple(PRESET_NAMES)
    alphabet_sizes = ALLOWED_ALPHABET_SIZES

    def add_arguments(self, ap: argparse.ArgumentParser):
        super().add_arguments(ap)
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
        ap.add_argument(
            "--no-scramble", action="store_true",
            help=(
                "Nutzdaten NICHT verwuerfeln. Standardmaessig werden die Datensymbole "
                "jeder Zeile mit einem aus der Dokument-ID abgeleiteten Pseudo-Zufallsstrom "
                "XOR-verknuepft, damit keine langen Folgen gleicher Zeichen entstehen "
                "(die OCR haeufig falsch zaehlt). Nur fuer Vergleiche/Debugging."
            ),
        )

    def _width_help_suffix(self, text: str) -> str:
        return text + " Mit --columns die Breite JEDER Spalte."

    def _lines_help_columns(self) -> str:
        return ("; mit --columns die Zeilen NACH dem Aufteilen in Spalten (eine Seite fasst "
                "dann --lines x --columns Nutzdatenzeilen)")

    def validate(self, ap: argparse.ArgumentParser, args):
        super().validate(ap, args)
        if args.columns < 1:
            ap.error("--columns muss >= 1 sein")

    def scrambled(self, args) -> bool:
        return not args.no_scramble

    def columns(self, args) -> int:
        return args.columns


class EncodeOptionsV5(EncodeOptionsV4):
    """Formatversion 5: Optionen wie 4 (nur das Zeilen-CRC bei 512er-Alphabeten aendert sich)."""

    version = 5


class EncodeOptionsV6(EncodeOptionsV5):
    """Formatversion 6: Optionen wie 5; --width ist nicht mehr durch die Alphabetgroesse begrenzt."""

    version = 6


ENCODE_OPTIONS = {o.version: o for o in (EncodeOptionsV3(), EncodeOptionsV4(), EncodeOptionsV5(), EncodeOptionsV6())}
assert set(ENCODE_OPTIONS) == set(FORMATS), "jede Formatversion braucht eine Optionsklasse"
