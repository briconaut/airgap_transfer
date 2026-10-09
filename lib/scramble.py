"""
Verwuerfelung (Scrambling) der Nutzdatensymbole vor dem RS-Kodieren.

Warum: Komprimierte oder Base64-artige Daten enthalten oft lange Folgen
gleicher Symbole ("qqqqqqqqqqqq", "AAAAAAAAAAAA"). Genau solche Folgen zaehlt
eine OCR (oder ein Mensch) gern falsch - ein Zeichen zu viel/zu wenig
verschiebt den Rest der Zeile, und Reed-Solomon sieht dann Dutzende Fehler
statt einem. XOR mit einem Pseudo-Zufallsstrom zerstoert solche Folgen.

Schluesselstrom: pro Nutzdatenzeile unabhaengig (wahlfreier Zugriff, der
Decoder kann jede Zeile einzeln entwuerfeln), abgeleitet aus der
Dokument-ID (steht ohnehin im Header) und der Zeilennummer:
    SHAKE-256(document_id || zeilennummer als uint64 big-endian)
je 2 Byte pro Symbol, maskiert auf bits_per_symbol Bit (gleichverteilt, da
2^bits_per_symbol ein Teiler von 2^16 ist). XOR ist selbstinvers - dieselbe
Funktion verwuerfelt und entwuerfelt.

Gearbeitet wird auf den Daten-SYMBOLEN eines Blocks (k Stueck), nicht auf
den Dateibytes: CRC32 und RS beziehen sich damit auf die verwuerfelten
Symbole (das, was tatsaechlich auf dem Bildschirm steht), entwuerfelt wird
erst nach erfolgreicher RS-Korrektur.
"""
import hashlib
import struct


def scramble_block(block, line_number: int, document_id: bytes, bits_per_symbol: int):
    """Verwuerfelt (bzw. entwuerfelt - XOR ist selbstinvers) die Datensymbole
    `block` der Nutzdatenzeile `line_number`. Rueckgabe: neue Liste."""
    mask = (1 << bits_per_symbol) - 1
    stream = hashlib.shake_256(document_id + struct.pack(">Q", line_number)).digest(2 * len(block))
    return [s ^ (((stream[2 * i] << 8) | stream[2 * i + 1]) & mask) for i, s in enumerate(block)]
