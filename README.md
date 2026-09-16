# Air-Gap Text-Encoder/-Decoder

Encoder/Decoder-Paar zum Transport einer Datei über einen rein optischen
Kanal (Linux-Text-Terminal → Screenshot → OCR) oder per Copy/Paste über die
Windows-Zwischenablage, ohne USB/Netzwerk. Reines Python 3.10,
**keine Zusatzpakete** (nur Standardbibliothek).

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
| `-o/--output` | Ausgabedatei (optional, sonst stdout). Enthält ein `*`, siehe "Seitenteilung" unten |
| `--width` | Gesamte Zeilenbreite **inkl.** Zeilennummer-, CRC- und Paritätsfeld (Default 100) |
| `--preset` | Eingebautes Alphabet-Preset: `default` (64 Zeichen, konfusionsarm), `ascii64` (64 Zeichen, alle Buchstaben+Ziffern), `latin128` (128 Zeichen, ASCII + Latin-1-Diakritika, GF128), `utf8128` (128 Zeichen, ASCII + Latin-Extended-A-Diakritika, GF128), `utf16le128`/`utf16le256` (siehe "UTF-16LE-Presets" unten). Schließt `--alphabet` aus |
| `--alphabet` | Custom-Alphabet-String (Länge muss 32/64/128/256 sein). Schließt `--preset` aus. Default: eingebautes 64er-Preset |
| `--redundancy` | Parität in % von k (Default 20.0) |
| `--parity-symbols` | Absolute Paritätssymbolzahl r, statt `--redundancy` |
| `--lines` | Nutzdatenzeilen pro Seite (siehe "Seitenteilung" unten). Default: keine Seitenteilung |
| `--filename` | Im Header gespeicherter Name (Default: Basisname der Eingabedatei) |

### decode.py

| Parameter | Bedeutung |
|---|---|
| `input` | OCR-Text-/Seiten-Datei(en) (optional, sonst stdin). Mehrere Dateien und/oder Glob-Patterns (z.B. `"seite_*.txt"`) sind erlaubt |
| `-o/--output` | Basisname der Ausgabe (Default `output`) |
| `--error-report-format` | `text` (Default) oder `json` |

**Seitenteilung:** `encode.py --lines N` teilt die Nutzdaten in Seiten zu je
N Zeilen auf. Jede Seite trägt eine **vollständige eigene Kopie** des
Headers (inkl. 3-fach wiederholter Praeambel) plus vier zusätzliche Felder:
Gesamtzahl der Seiten, Nummer dieser Seite, eine CRC32-Prüfsumme dieser
Seite sowie eine zufällige, für alle Seiten einer Datei gemeinsame
Dokument-ID. Zeilennummern in den Nutzdatenzeilen bleiben dabei global über
die ganze Datei (unverändert durch die Seitenteilung).

Ohne `*` im `-o`-Dateinamen (oder bei Ausgabe nach stdout) werden alle
Seiten in **einem einzigen Textstrom** hintereinander ausgegeben, getrennt
durch 3 Leerzeilen — bei den UTF-16LE-Presets (siehe unten) erscheint dabei
nur **ein** BOM ganz am Anfang des Gesamtstroms, nicht pro Seite.

Enthält der `-o`-Dateiname dagegen ein `*` (z.B. `-o
my/dir/output*.enc --lines 2000`), schreibt `encode.py` **eine eigene Datei
pro Seite**: `*` wird durch die 1-basierte Seitennummer ersetzt, mit
optimaler Nullauffüllung (z.B. `output01.enc`, `output02.enc`, … bei 10-99
Seiten; `output1.enc` bei nur 1 Seite; `output001.enc` ab 100 Seiten). Jede
dieser Dateien ist ein eigenständiger, vollständiger Strom und bekommt bei
den UTF-16LE-Presets **ihr eigenes BOM** — anders als beim einzelnen
Gesamtstrom oben.

`decode.py` akzeptiert Seiten in drei Formen, beliebig kombinierbar:
alle Seiten in einer Datei/stdin (auch in **gemischter Reihenfolge**),
mehrere einzelne Dateien als separate Argumente, oder ein Glob-Pattern
(z.B. `python3 decode.py "seite_*.txt" -o wiederhergestellt`). Der Header
je Seite ist wie die Präambel nicht redundant über mehrere Seiten
abgesichert: ist der Header einer einzelnen Seite nicht lesbar, wird nur
diese Seite übersprungen (ihre Zeilen zählen als fehlend) — erst wenn
**keine einzige** Seite einen lesbaren Header liefert, bricht der Decoder
komplett ab. Seiten mit unterschiedlichen Dokument-IDs im selben Aufruf
führen zum Abbruch (kein automatisches Aufteilen auf mehrere Dokumente).

**Ausgabe:**
- Alles fehlerfrei rekonstruierbar → eine Datei `<output>`, SHA-256 gegen den
  Header geprüft.
- Fehler/Lücken vorhanden → `<output>.part1` (sauberer Anfang bis zur ersten
  fehlerhaften Zeile), `<output>.part2` (Rest, inkl. nicht rekonstruierbarer
  Stellen als Nullbytes), `<output>.errors.txt`/`.json` (alle fehlerhaften
  Zeilen mit Grund). SHA-256-Prüfung wird in diesem Fall übersprungen.

**UTF-16LE-Presets:** `latin128`/`utf8128` unterscheiden sich nur in der
*Zeichenauswahl* — die Ausgabedatei bleibt in jedem Fall UTF-8. `utf16le128`
(identische 128 Zeichen wie `utf8128`) und `utf16le256` (neues 256-Zeichen-
Set aus Box-Drawing-/Block-Element-/Geometrie-Symbolen, GF256, ca. 14%
dichter, NICHT OCR-kuratiert) sind anders: hier schreibt `encode.py` die
Ausgabedatei selbst als **echtes UTF-16LE** (mit BOM), statt nur andere
Unicode-Zeichen in einer UTF-8-Datei zu benutzen. Gedacht für den
Zwischenablage-Transfer, da Windows' natives Zwischenablage-Textformat
(CF_UNICODETEXT) selbst UTF-16LE ist — vermeidet den UTF-8↔UTF-16-Umweg dort.

`decode.py` erkennt UTF-8 vs. UTF-16LE **pro Quelle automatisch** an den
Rohbytes, ohne die Datei neu zu öffnen und ohne dass ein BOM zwingend nötig
wäre (funktioniert auch bei einer aus der Gesamtdatei herausgeschnittenen
Einzelseite ohne führendes BOM, siehe "Seitenteilung" oben): das Header-
Alphabet ist immer reines 7-Bit-ASCII, und `0x00` ist in jedem Alphabet
verboten (siehe `alphabets.py`) — ein UTF-8-Strom kann also nie ein rohes
`0x00`-Byte enthalten, während ASCII als UTF-16LE immer `Byte, 0x00`-Paare
erzeugt. Das erkannte Encoding wird zusätzlich gegen ein explizites
`text_encoding`-Feld im Header jeder Seite geprüft; bei Widerspruch wird nur
diese eine Seite übersprungen (wie bei jedem anderen Seiten-Header-Fehler).

**Zwischenablage statt OCR:** Bei sehr großen Dateien reicht ein einzelner
Copy/Paste-Vorgang oft nicht aus, sodass mehrere Teilstücke nacheinander
eingefügt werden müssen. Geht dabei an einer Nahtstelle der Zeilenumbruch
verloren, verschmelzen zwei (oder mehr) Nutzdatenzeilen zu einer Roh-Zeile.
`decode.py` erkennt das automatisch (jede Nutzdatenzeile hat eine bekannte,
feste Länge) und teilt betroffene Roh-Zeilen vor der Weiterverarbeitung
wieder auf — ein Hinweis dazu erscheint auf stderr. Das deckt nur den
Nutzdatenbereich ab; verschmilzt ausgerechnet die (sehr kurze) Präambel/
Header-Zeile mit einer Nachbarzeile, gilt weiterhin das bestehende
Restrisiko für den Header-Bereich (siehe unten).

## Wichtiger Fakt zur Fehlerkorrektur

**r Paritätssymbole korrigieren nicht r Fehler, sondern nur `floor(r/2)`
Fehler pro Zeile** (Reed-Solomon ohne bekannte Fehlerpositionen). Bei
`--redundancy 20` und z. B. k=50/r=10 heißt das: bis zu 5 verfälschte Zeichen
pro Zeile werden repariert, ab 6 wird die Zeile als nicht korrigierbar
verworfen (nie stillschweigend falsch decodiert — das ist getestet, siehe
`test_rs_codec.py`).

## Format (Kurzfassung)

```
Je Seite (mindestens 1 - ohne --lines besteht die ganze Datei aus einer Seite):
  Zeile 1:         Präambel (5 Zeichen, Header-Alphabet): Anzahl Header-Zeilen + Prüfsumme
  Zeilen 2..N:      Header, 3-fach wiederholt, je mit [Kopie-Idx][Zeilen-Idx][Inhalt]
                    (Formatversion, Alphabet, Dokument-ID, Seitenzahl/-nummer/-prüfsumme,
                    k, r, Dateigröße, SHA-256, Dateiname, ...)
  Zeilen N+1..Ende: Nutzdaten dieser Seite, je [Zeilennummer][CRC32][RS-Codewort(k+r)]
(3 Leerzeilen zwischen den Seiten, Zeilennummern global über die ganze Datei)
```

Header- und Zeilennummer-/CRC-Felder stehen **immer** im festen 64-Symbol-
Header-Alphabet, unabhängig vom für die Nutzdaten gewählten Alphabet.

## Bekannte, bewusst akzeptierte Restrisiken

Diese Punkte wurden im Entwurfsprozess bewusst so entschieden (Kompromiss
Einfachheit/Risiko) und sind keine Bugs:

- **Präambel** ist nicht 3-fach abgesichert (nur 1 Prüfsymbol zur reinen
  Erkennung). Geht die Präambel-Zeile einer Seite komplett verloren, ist
  diese eine Seite nicht mehr lesbar (ihre Zeilen zählen als fehlend) - bei
  Seitenteilung betrifft das nur diese eine Seite, nicht die ganze Datei.
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
