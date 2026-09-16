"""
Alphabete fuer die Terminal-Darstellung.

- HEADER_ALPHABET: fest, IMMER 64 konfusionsarme, druckbare 7-Bit-ASCII-
  Zeichen. Wird fuer Praeambel, Header, Zeilennummer und CRC-Feld benutzt,
  unabhaengig davon, welches Alphabet fuer die Nutzdaten gewaehlt wurde.
- Nutzdaten-Alphabet: Default = dasselbe wie HEADER_ALPHABET, ueberschreibbar
  per --alphabet. Muss eine Zweierpotenz-Laenge haben (32/64/128/256).
"""

ALLOWED_ALPHABET_SIZES = (32, 64, 128, 256)

# 64 Zeichen, bewusst ohne bekannte Verwechslungspaare:
#   Ziffern ohne 0/1/5/8 (Verwechslung mit O/I,l/S,s/B)
#   Grossbuchstaben ohne B,I,O,S
#   Kleinbuchstaben ohne b,i,l,o,s
#   + 15 Sonderzeichen, die weder sich selbst noch obige Zeichen imitieren
HEADER_ALPHABET = (
    "234679"
    "ACDEFGHJKLMNPQRTUVWXYZ"
    "acdefghjkmnpqrtuvwxyz"
    "+-.:;=_#%&*@^~!"
)
assert len(HEADER_ALPHABET) == 64, f"Header-Alphabet hat {len(HEADER_ALPHABET)} statt 64 Zeichen"
assert len(set(HEADER_ALPHABET)) == 64, "Header-Alphabet enthaelt Duplikate"

DEFAULT_ALPHABET = HEADER_ALPHABET

# ---------------------------------------------------------------------------
# Eingebaute Nutzdaten-Presets
# ---------------------------------------------------------------------------

# ascii64: alle Buchstaben (A-Z, a-z), alle Ziffern (0-9) und +/-.
# Schliesst die im Default-Preset ausgeschlossenen Verwechslungspaare
# (0/O, 1/I/l, 5/S, 8/B usw.) bewusst EIN — nutzt Terminals/Fonts, die diese
# Paare zuverlässig darstellen. Gleiche Dichte wie Default (GF(64), 6 Bit/Symbol),
# aber breitere Zeichenauswahl fuer rein 7-Bit-ASCII-Umgebungen.
PRESET_ASCII64 = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "+-"
)
assert len(PRESET_ASCII64) == 64 and len(set(PRESET_ASCII64)) == 64

# latin128: alle 94 druckbaren 7-Bit-ASCII-Zeichen (0x21-0x7E) plus 34
# gut OCR-erkennbare Latin-1-Zeichen (0xC0-0xEA, gezielt ausgewaehlt).
# Ausgeschlossen: Zeichen mit zu grosser Aehnlichkeit zu ASCII-Basiszeichen
# (z.B. Ì/Í/Î/Ï wegen I, Ý/ý wegen Y, ß wegen B/ss).
# Nutzt GF(128), 7 Bit/Symbol — ca. 17% mehr Nutzdaten pro Zeile als 64er-Presets.
# Voraussetzung: Terminal und OCR unterstuetzen UTF-8/Latin-1.
PRESET_LATIN128 = (
    # 94 druckbare 7-Bit-ASCII-Zeichen (0x21–0x7E)
    "!\"#$%&'()*+,-./"
    "0123456789"
    ":;<=>?@"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "[\\]^_`"
    "abcdefghijklmnopqrstuvwxyz"
    "{|}~"
    # 34 Latin-1-Zeichen: Grossbuchstaben mit klar sichtbarem Diakritikum
    "\xC0\xC1\xC2\xC3\xC4\xC5"   # À Á Â Ã Ä Å
    "\xC6\xC7"                    # Æ Ç
    "\xC8\xC9\xCA\xCB"            # È É Ê Ë
    "\xD1"                        # Ñ
    "\xD2\xD3\xD4\xD5\xD6\xD8"   # Ò Ó Ô Õ Ö Ø
    "\xDA\xDB\xDC\xDE"            # Ú Û Ü Þ
    # Kleinbuchstaben mit klar sichtbarem Diakritikum
    "\xE0\xE1\xE2\xE3\xE4\xE5"   # à á â ã ä å
    "\xE6\xE7"                    # æ ç
    "\xE8\xE9\xEA"                # è é ê
)
assert len(PRESET_LATIN128) == 128 and len(set(PRESET_LATIN128)) == 128

PRESETS = {
    "default":   DEFAULT_ALPHABET,
    "ascii64":   PRESET_ASCII64,
    "latin128":  PRESET_LATIN128,
}

PRESET_NAMES = list(PRESETS)


class Alphabet:
    """Kapselt ein Alphabet: Zeichen <-> Symbolwert (Index) + Bit-Slicing."""

    def __init__(self, chars: str):
        if len(chars) not in ALLOWED_ALPHABET_SIZES:
            raise ValueError(
                f"Alphabetlaenge muss eine Zweierpotenz aus {ALLOWED_ALPHABET_SIZES} sein, "
                f"nicht {len(chars)} (arbitraere Basiskonvertierung wurde bewusst verworfen, "
                f"siehe Design-Notizen: zu langsam/unvorhersehbar in reinem Python)."
            )
        if len(set(chars)) != len(chars):
            raise ValueError("Alphabet enthaelt doppelte Zeichen")
        for c in chars:
            # Nur Steuerzeichen/Whitespace grundsaetzlich verboten (Zeilenformat
            # ist positionsfest ohne Trennzeichen, siehe framing.py). Das eingebaute
            # 64er-Preset bleibt bewusst 7-Bit-ASCII; ein CUSTOM-Alphabet darf
            # bewusst darueber hinausgehen, wenn das Zielterminal das nachweislich
            # kann (siehe Design-Notizen Runde 5/9) - das ist kein Bug, sondern
            # der eigentliche Zweck des --alphabet-Parameters.
            if ord(c) < 33 or c.isspace():
                raise ValueError(f"Zeichen {c!r} ist ein Steuerzeichen/Whitespace - nicht erlaubt")
        self.chars = chars
        self.size = len(chars)
        self.bits_per_symbol = self.size.bit_length() - 1  # size ist Zweierpotenz
        self._char_to_val = {c: i for i, c in enumerate(chars)}

    def symbols_to_text(self, symbols) -> str:
        return "".join(self.chars[s] for s in symbols)

    def text_to_symbols(self, text: str):
        try:
            return [self._char_to_val[c] for c in text]
        except KeyError as e:
            raise ValueError(f"Zeichen {e.args[0]!r} gehoert nicht zu diesem Alphabet") from e

    # -- Bit-Slicing: Bytes -> Symbole und zurueck ---------------------------

    def bytes_to_symbols(self, data: bytes):
        """Reines Bit-Slicing (kein Bignum-Basiswechsel). Der Akkumulator wird
        nach jedem Byte auf 24 Bit begrenzt, damit er NICHT unbegrenzt waechst
        (das war der Performance-Bug im ersten Benchmark-Versuch)."""
        bits = self.bits_per_symbol
        mask = (1 << bits) - 1
        out = []
        acc = 0
        acc_bits = 0
        for b in data:
            acc = ((acc << 8) | b) & 0xFFFFFF
            acc_bits += 8
            while acc_bits >= bits:
                acc_bits -= bits
                out.append((acc >> acc_bits) & mask)
        if acc_bits > 0:
            pad = bits - acc_bits
            out.append((acc << pad) & mask)
        return out

    def symbols_to_bytes(self, symbols, expected_length: int | None = None) -> bytes:
        """Kehrt bytes_to_symbols um. Ohne expected_length wird die Byteanzahl
        exakt ueber floor(len(symbols)*bits/8) zurueckgerechnet (funktioniert,
        weil bytes_to_symbols pro Aufruf hoechstens (bits-1) Padding-Bits < 1
        Byte anhaengt). Mit expected_length wird stattdessen hart auf diese
        Laenge abgeschnitten (fuer die Nutzdaten, wo die wahre Dateigroesse
        aus dem Header bekannt ist)."""
        bits = self.bits_per_symbol
        acc = 0
        acc_bits = 0
        out = bytearray()
        for s in symbols:
            acc = ((acc << bits) | s) & 0xFFFFFF  # Akkumulator begrenzt halten (siehe Benchmark-Notizen: unbegrenztes Wachstum -> O(n^2))
            acc_bits += bits
            while acc_bits >= 8:
                acc_bits -= 8
                out.append((acc >> acc_bits) & 0xFF)
        if expected_length is not None:
            return bytes(out[:expected_length])
        return bytes(out)


def make_alphabet(preset: str | None = None,
                  custom_chars: str | None = None) -> tuple["Alphabet", bool]:
    """Gibt (Alphabet, is_custom) zurueck.

    is_custom=False nur fuer das 'default'-Preset (Alphabet-Zeichen werden dann
    NICHT im Header gespeichert, weil decode.py das Default kennt). Alle anderen
    Presets und Custom-Strings setzen is_custom=True und speichern die Zeichen
    im Header, damit decode.py sie ohne zusaetzliche Parameter rekonstruieren kann.

    Rangfolge: custom_chars > preset > 'default'.
    """
    if custom_chars is not None:
        return Alphabet(custom_chars), True
    name = preset or "default"
    if name not in PRESETS:
        raise ValueError(
            f"Unbekanntes Preset {name!r}. Gueltig: {PRESET_NAMES}"
        )
    return Alphabet(PRESETS[name]), (name != "default")
