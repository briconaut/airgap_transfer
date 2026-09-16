# Air-Gap Text-Encoder/-Decoder

Encoder/Decoder-Paar zum Transport einer Datei über einen rein optischen
Kanal (Linux-Text-Terminal → Screenshot → OCR), ohne USB/Netzwerk.
Reines Python 3.10, **keine Zusatzpakete** (nur Standardbibliothek).

Screenshot-Erstellung, OCR und Seitenaufteilung/Paging sind **nicht** Teil
dieses Tools — beide Programme sind reine Text-in/Text-out-Filter und lassen
sich über stdin/stdout in eine externe Pipeline einhängen.

## Nutzung

```bash
# Encodieren
python3 encode.py geheim.bin -o geheim.txt --width 100 --redundancy 20
cat geheim.bin | python3 encode.py --width 100 > geheim.txt   # via stdin/stdout

# Decodieren (Eingabe = OCR-Rohtext)
python3 decode.py ocr_output.txt -o wiederhergestellt
python3 decode.py -o wiederhergestellt < ocr_output.txt
```

### encode.py

| Parameter | Bedeutung |
|---|---|
| `input` | Eingabedatei (optional, sonst stdin) |
| `-o/--output` | Ausgabedatei (optional, sonst stdout) |
| `--width` | Gesamte Zeilenbreite **inkl.** Zeilennummer-, CRC- und Paritätsfeld (Default 100) |
| `--preset` | Eingebautes Alphabet-Preset: `default` (64 Zeichen, konfusionsarm), `ascii64` (64 Zeichen, alle Buchstaben+Ziffern), `latin128` (128 Zeichen, ASCII + Latin-1-Diakritika, GF128), `utf8128` (128 Zeichen, ASCII + Latin-Extended-A-Diakritika, GF128). Schließt `--alphabet` aus |
| `--alphabet` | Custom-Alphabet-String (Länge muss 32/64/128/256 sein). Schließt `--preset` aus. Default: eingebautes 64er-Preset |
| `--redundancy` | Parität in % von k (Default 20.0) |
| `--parity-symbols` | Absolute Paritätssymbolzahl r, statt `--redundancy` |
| `--filename` | Im Header gespeicherter Name (Default: Basisname der Eingabedatei) |

### decode.py

| Parameter | Bedeutung |
|---|---|
| `input` | OCR-Textdatei (optional, sonst stdin) |
| `-o/--output` | Basisname der Ausgabe (Default `output`) |
| `--error-report-format` | `text` (Default) oder `json` |

**Ausgabe:**
- Alles fehlerfrei rekonstruierbar → eine Datei `<output>`, SHA-256 gegen den
  Header geprüft.
- Fehler/Lücken vorhanden → `<output>.part1` (sauberer Anfang bis zur ersten
  fehlerhaften Zeile), `<output>.part2` (Rest, inkl. nicht rekonstruierbarer
  Stellen als Nullbytes), `<output>.errors.txt`/`.json` (alle fehlerhaften
  Zeilen mit Grund). SHA-256-Prüfung wird in diesem Fall übersprungen.

## Wichtiger Fakt zur Fehlerkorrektur

**r Paritätssymbole korrigieren nicht r Fehler, sondern nur `floor(r/2)`
Fehler pro Zeile** (Reed-Solomon ohne bekannte Fehlerpositionen). Bei
`--redundancy 20` und z. B. k=50/r=10 heißt das: bis zu 5 verfälschte Zeichen
pro Zeile werden repariert, ab 6 wird die Zeile als nicht korrigierbar
verworfen (nie stillschweigend falsch decodiert — das ist getestet, siehe
`test_rs_codec.py`).

## Format (Kurzfassung)

```
Zeile 1:         Präambel (5 Zeichen, Header-Alphabet): Anzahl Header-Zeilen + Prüfsumme
Zeilen 2..N:      Header, 3-fach wiederholt, je mit [Kopie-Idx][Zeilen-Idx][Inhalt]
                  (Formatversion, Alphabet, k, r, Dateigröße, SHA-256, Dateiname, ...)
Zeilen N+1..Ende: Nutzdaten, je [Zeilennummer][CRC32][RS-Codewort(k Daten + r Parität)]
```

Header- und Zeilennummer-/CRC-Felder stehen **immer** im festen 64-Symbol-
Header-Alphabet, unabhängig vom für die Nutzdaten gewählten Alphabet.

## Bekannte, bewusst akzeptierte Restrisiken

Diese Punkte wurden im Entwurfsprozess bewusst so entschieden (Kompromiss
Einfachheit/Risiko) und sind keine Bugs:

- **Präambel** ist nicht 3-fach abgesichert (nur 1 Prüfsymbol zur reinen
  Erkennung). Geht die allererste Zeile der Datei komplett verloren, ist die
  Datei nicht mehr lesbar.
- **Zeilennummer** selbst ist nicht durch das CRC geschützt (CRC deckt nur
  die Nutzdaten ab). Ein OCR-Fehler ausgerechnet in der Zeilennummer könnte
  eine Zeile theoretisch an die falsche Position einsortieren, statt als
  Lücke erkannt zu werden.
- **Geht eine ganze Header-Zeile verloren** (nicht nur eine Kopie davon,
  sondern eine komplette Kopie *und* die Zählung verschiebt sich), kann sich
  in seltenen Fällen die Grenze zwischen Header- und Nutzdatenbereich um eine
  Zeile verschieben. Das führt meist zu einem erkannten Header-Fehler (harter
  Abbruch), im ungünstigsten Fall zum Verlust der ersten Nutzdatenzeile ohne
  Fehlermeldung. Betrifft nur den sehr kleinen Header-Bereich (typischerweise
  1-3 Zeilen von insgesamt tausenden).
- **RS-Codewort über der Korrekturkapazität**: in extrem seltenen Fällen kann
  eine Zeile mit mehr Fehlern als `floor(r/2)` fälschlich als "korrigierbar"
  erscheinen (siehe `test_rs_codec.py`, dort wird das aktiv gegengetestet und
  war in allen Testläufen 0/20 Fällen). Das zusätzliche CRC32-Feld fängt
  auch diesen Restfall in der Praxis ab (Kategorie "fragwürdig korrigiert").

## Performance-Hinweis

Reine Python-Implementierung ohne Zusatzpakete, bewusst ohne Parallelisierung
(Batch-Verarbeitung, nicht zeitkritisch). Ein früher Benchmark der reinen
Reed-Solomon-Mathematik ergab ca. 14 Minuten Encode-Zeit für 300 MB bei
r=32; die tatsächliche Performance des fertigen Tools bei dieser
Größenordnung wurde auf Wunsch nicht separat validiert. Ein während der
Implementierung gefundener und behobener Bug (unbegrenzt wachsender
Bit-Akkumulator beim Byte↔Symbol-Slicing) hätte ohne Fix zu einer
Performance-Katastrophe geführt (quadratisch statt linear) — nach dem Fix
lief ein 300-KB-Testfall in ca. 1 Sekunde statt vorher >20 Sekunden. Vor dem
produktiven Einsatz mit dreistelligen MB empfiehlt sich ein eigener Testlauf
mit repräsentativer Dateigröße.

## Tests

```bash
python3 test_rs_codec.py   # RS-Codec: alle Feldgrößen, Korrektur bis exakt floor(r/2), Ablehnung darüber
python3 test_e2e.py        # Encoder+Decoder: sauberer Roundtrip, Korrektur, Zeilenverlust, Custom-Alphabet
```
