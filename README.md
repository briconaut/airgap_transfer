# Air-Gap Text-Encoder/-Decoder

Encoder/Decoder-Paar zum Transport einer Datei über einen rein optischen
Kanal (Linux-Text-Terminal → Screenshot → OCR) oder per Copy/Paste über die
Windows-Zwischenablage, ohne USB/Netzwerk. Reines Python 3.10,
**keine Zusatzpakete** (nur Standardbibliothek).

Screenshot-Erstellung und OCR sind **nicht** Teil dieses Tools — Encoder
und Decoder sind reine Text-in/Text-out-Filter und lassen sich über
stdin/stdout in eine externe Pipeline einhängen.

Werkzeuge (alle in `bin/`):

| Skript | Zweck |
|---|---|
| `encode.py` | Datei → fehlerkorrigierender Text (optional in Seiten/Spalten aufgeteilt) |
| `decode.py` | OCR-/Zwischenablage-Text → Datei, mit Fehlerbericht |
| `reframe.py` | Vorhandenen Base64-Text mit demselben Framing versehen (siehe unten) |
| `archive2git.py` | Übertragenes `tar.bz2`-Archiv direkt als Git-Commit importieren (siehe unten) |

## Nutzung

```bash
# Encodieren
python3 bin/encode.py geheim.bin -o geheim.txt --width 100 --redundancy 20
cat geheim.bin | python3 bin/encode.py --width 100 > geheim.txt   # via stdin/stdout

# Decodieren (Eingabe = OCR-Rohtext)
python3 bin/decode.py ocr_output.txt -o wiederhergestellt
python3 bin/decode.py -o wiederhergestellt < ocr_output.txt
```

Die Skripte in `bin/` sind dünne Startskripte für das Paket `airgap/`;
gleichwertig ist `python3 -m airgap.encode` bzw. `python3 -m airgap.decode`
aus dem Projektverzeichnis.

### encode.py

| Parameter | Bedeutung |
|---|---|
| `input` | Eingabedatei (optional, sonst stdin) |
| `-o/--output` | Ausgabedatei (optional, sonst stdout). Enthält ein `*`, siehe "Seitenteilung" unten |
| `--width` | Gesamte Zeilenbreite **inkl.** Zeilennummer-, CRC- und Paritätsfeld (Default 100) |
| `--preset` | Eingebautes Alphabet-Preset: `default` (64 Zeichen, konfusionsarm), `ascii64` (64 Zeichen, alle Buchstaben+Ziffern), `latin128` (128 Zeichen, ASCII + Latin-1-Diakritika, GF128), `utf8128` (128 Zeichen, ASCII + Latin-Extended-A-Diakritika, GF128), `utf16le128`/`utf16le256`/`utf32le128`/`utf32le256`/`utf32le512`/`utf32be512` (siehe "UTF-16LE-/UTF-32-Presets" unten). Schließt `--alphabet` aus |
| `--alphabet` | Custom-Alphabet-String (Länge muss 32/64/128/256/512 sein). Schließt `--preset` aus. Default: eingebautes 64er-Preset |
| `--redundancy` | Parität in % von k (Default 20.0) |
| `--parity-symbols` | Absolute Paritätssymbolzahl r, statt `--redundancy` |
| `--lines` | Nutzdatenzeilen pro Seite (siehe "Seitenteilung" unten); mit `--columns` die Zeilen nach dem Aufteilen in Spalten, eine Seite fasst dann `--lines` × `--columns` Nutzdatenzeilen. Default: keine Seitenteilung |
| `--columns` | Spaltenzahl pro Seite (Default 1). Die Nutzdatenzeilen jeder Seite werden spaltenweise verteilt (erst Spalte 1 von oben nach unten, dann Spalte 2, …), jede Spalte ist so breit wie eine Nutzdatenzeile (höchstens `--width`), Trenner ist ` ### `. Präambel und Header bleiben einspaltig. `decode.py` erkennt das Layout automatisch |
| `--filename` | Im Header gespeicherter Name (Default: Basisname der Eingabedatei) |
| `--no-scramble` | Nutzdaten nicht verwürfeln (siehe "Folgen gleicher Zeichen" unten). Default: verwürfelt |
| `--progress` | Fortschrittsbalken auf stderr anzeigen |
| `--show-preset <preset>` | Gibt alle Zeichen aus, die eine mit diesem Preset kodierte Datei enthalten kann (Header-Alphabet + Preset-Alphabet), als eine UTF-8-Zeile auf stdout, und beendet sich, ohne etwas zu kodieren. Gedacht für Tesseracts `tessedit_char_whitelist` |

### decode.py

| Parameter | Bedeutung |
|---|---|
| `input` | OCR-Text-/Seiten-Datei(en) (optional, sonst stdin). Mehrere Dateien und/oder Glob-Patterns (z.B. `"seite_*.txt"`) sind erlaubt |
| `-o/--output` | Basisname der Ausgabe (Default `output`) |
| `--error-report-format` | `text` (Default) oder `json` |
| `--header` | Nur die Header aller Seiten jeder Eingabedatei ausgeben (nach stdout); Nutzdaten werden nicht verarbeitet, `-o` wird ignoriert |

`encode.py` meldet Layout (k, r, Zeilenzahl) und Hinweise auf stderr.
Exitcodes: `0` Erfolg, `1` Eingabedatei nicht lesbar, `2` ungültige
Parameter (z.B. `--width` zu klein, ungültiges Alphabet, mehr als 65535
Seiten).

### reframe.py

Liest **Base64-Text von stdin** (z.B. die Ausgabe von `base64`) und gibt ihn
mit Header, Zeilennummern, CRC32 und Reed-Solomon-Parität aus — für den Fall,
dass auf der Quellseite schon Base64-Text vorliegt. `decode.py` liefert
daraus exakt den Base64-Text zurück (ohne Zeilenumbrüche und `=`-Padding),
der dann außerhalb mit `base64 -d` zur Originaldatei wird:

```bash
base64 geheim.bin | python3 bin/reframe.py --width 100 -o geheim_framed.txt
python3 bin/decode.py geheim_framed.txt -o geheim.b64
base64 -d geheim.b64 > geheim.bin
```

Parameter: `-o/--output`, `--width`, `--redundancy`, `--parity-symbols`,
`--filename`, `--progress` (Bedeutung wie bei `encode.py`). Immer
Default-Alphabet, eine einzige Seite, keine Verwürfelung. Jedes Base64-Zeichen
wird als ASCII-Byte kodiert; gegenüber `encode.py` auf der Binärdatei kostet
das rund ein Drittel mehr Zeilen. `=` ist nur ganz am Ende erlaubt.
Exitcodes: `0` Erfolg, `1` ungültige Eingabe (kein Base64, leer), `2`
ungültige Parameter.

### archive2git.py

Importiert einen `tar.bz2`-Strom von stdin als Commit in das Git-Repository
des aktuellen Verzeichnisses, ohne Archiv oder Dateien auf die Platte zu
schreiben — gedacht für den Transfer eines Quellcode-Stands
(`tar -cjf - projekt | encode.py …` → … → `decode.py` → `archive2git.py`):

```bash
cd ziel-repo
python3 /pfad/zu/bin/archive2git.py --strip projekt < projekt.tar.bz2
git checkout -f HEAD -- .     # Arbeitsverzeichnis aus dem neuen Commit füllen
```

| Parameter | Bedeutung |
|---|---|
| `--strip DIR` | Führendes Verzeichnis von jedem Pfad entfernen (mehrfach angebbar; die erste passende Regel gilt, Vergleich komponentenweise) |
| `-m/--message` | Commit-Message (Default `Import aus tar.bz2`) |

Der Baum des Commits entspricht exakt dem Archivinhalt. Auf einem leeren
Branch entsteht ein initialer Commit, sonst ein neuer Commit auf dem
bisherigen Stand (History bleibt erhalten). Index und Arbeitsverzeichnis
werden nicht verändert. Reguläre Dateien (inkl. Ausführbar-Bit), Symlinks und
Hardlinks werden übernommen; Verzeichnisse, Devices und FIFOs nicht (in Git
nicht abbildbar). Exitcodes: `0` Commit erstellt, `1` Fehler (kein
Git-Repository, kaputtes Archiv, …), `2` ungültige Parameter.

**Seitenteilung:** `encode.py --lines N` teilt die Nutzdaten in Seiten zu je
N Zeilen auf (mit `--columns C` zu je N Zeilen à C Spalten, also N × C
Nutzdatenzeilen). Jede Seite trägt eine **vollständige eigene Kopie** des
Headers (Präambel plus 3-fach wiederholter Header-Körper) plus vier zusätzliche Felder:
Gesamtzahl der Seiten, Nummer dieser Seite, eine CRC32-Prüfsumme dieser
Seite sowie eine zufällige, für alle Seiten einer Datei gemeinsame
Dokument-ID. Zeilennummern in den Nutzdatenzeilen bleiben dabei global über
die ganze Datei (unverändert durch die Seitenteilung).

Ohne `*` im `-o`-Dateinamen (oder bei Ausgabe nach stdout) werden alle
Seiten in **einem einzigen Textstrom** hintereinander ausgegeben, getrennt
durch 3 Leerzeilen — bei den UTF-16LE-/UTF-32-Presets (siehe unten) erscheint
dabei nur **ein** BOM ganz am Anfang des Gesamtstroms, nicht pro Seite.

Enthält der `-o`-Dateiname dagegen ein `*` (z.B. `-o
my/dir/output*.enc --lines 2000`), schreibt `encode.py` **eine eigene Datei
pro Seite**: `*` wird durch die 1-basierte Seitennummer ersetzt, mit
optimaler Nullauffüllung (z.B. `output01.enc`, `output02.enc`, … bei 10-99
Seiten; `output1.enc` bei nur 1 Seite; `output001.enc` ab 100 Seiten). Jede
dieser Dateien ist ein eigenständiger, vollständiger Strom und bekommt bei
den UTF-16LE-/UTF-32-Presets **ihr eigenes BOM** — anders als beim einzelnen
Gesamtstrom oben.

`decode.py` akzeptiert Seiten in drei Formen, beliebig kombinierbar:
alle Seiten in einer Datei/stdin (auch in **gemischter Reihenfolge**),
mehrere einzelne Dateien als separate Argumente, oder ein Glob-Pattern
(z.B. `python3 bin/decode.py "seite_*.txt" -o wiederhergestellt`). Der Header
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

**Exitcodes von `decode.py`:** `0` vollständig und SHA-256 OK, `1` Abbruch
(z.B. keine lesbare Seite), `2` ungültige Kommandozeile, `3` unvollständig
(`.part1`/`.part2` geschrieben), `4` alle Zeilen gelesen, aber SHA-256 stimmt
nicht. Der Fehlerbericht wird in jedem Fall geschrieben.

**UTF-16LE-/UTF-32-Presets:** `latin128`/`utf8128` unterscheiden sich nur in
der *Zeichenauswahl* — die Ausgabedatei bleibt in jedem Fall UTF-8. Sechs
Presets sind anders: hier schreibt `encode.py` die Ausgabedatei selbst in
einem breiteren Encoding (mit BOM), statt nur andere Unicode-Zeichen in
einer UTF-8-Datei zu benutzen:

| Preset | Zeichen | Encoding der Ausgabedatei |
|---|---|---|
| `utf16le128` | identisch zu `utf8128` (128, GF128) | UTF-16LE |
| `utf16le256` | neues 256er-Set: Box-Drawing/Block-Elements/Geometrie (GF256, ca. 14% dichter als 128er) | UTF-16LE |
| `utf32le128` | identisch zu `utf16le128` | UTF-32LE |
| `utf32le256` | identisch zu `utf16le256` | UTF-32LE |
| `utf32le512` | neues 512er-Set: Mathematical Alphanumeric Symbols, U+1D400+ (GF512, ca. 13% dichter als 256er) | UTF-32LE |
| `utf32be512` | identisch zu `utf32le512` | UTF-32BE |

`utf16le128`/`utf16le256` sind für den Zwischenablage-Transfer gedacht, da
Windows' natives Zwischenablage-Textformat (CF_UNICODETEXT) selbst UTF-16LE
ist — vermeidet den UTF-8↔UTF-16-Umweg dort. Die `utf32*`-Presets sind für
den Workflow `Get-Content -Raw -Encoding UTF32 datei.txt | Set-Clipboard`
bzw. `-Encoding BigEndianUTF32` in PowerShell gedacht (`Set-Clipboard` selbst
hat kein `-Encoding` — die Datei wird beim Lesen zu einem normalen
.NET-String, danach ganz normal UTF-16 auf der Zwischenablage). Der
eigentliche Mehrwert von `utf32le512`/`utf32be512` gegenüber den 256er-Presets
ist nicht die Dateigröße (UTF-32 braucht pro Zeichen mehr Bytes als UTF-16),
sondern das größere Alphabet: UTF-32 braucht anders als UTF-16LE keine
Surrogatpaare für Codepoints jenseits der Basic Multilingual Plane, was den
Pool an sicher nutzbaren Zeichen stark vergrößert. Das 512er-Zeichenset ist
nach denselben Kriterien wie `utf16le256` kuratiert (NFC-zerlegungssicher,
keine kombinierenden Zeichen), zusätzlich ohne jeden Emoji-präsentierten
Unicode-Block, um zu verhindern, dass eine Ziel-App beim Einfügen Zeichen
automatisch umschreibt (z.B. Variation-Selector-Zusatz).

`decode.py` erkennt UTF-8/UTF-16LE/UTF-32LE/UTF-32BE **pro Quelle
automatisch** an den Rohbytes, ohne die Datei neu zu öffnen und ohne dass ein
BOM zwingend nötig wäre (funktioniert auch bei einer aus der Gesamtdatei
herausgeschnittenen Einzelseite ohne führendes BOM, siehe "Seitenteilung"
oben): das Header-Alphabet ist immer reines 7-Bit-ASCII, und `0x00` ist in
jedem Alphabet verboten (siehe `airgap/alphabets.py`) — ein UTF-8-Strom kann also
nie ein rohes `0x00`-Byte enthalten, während ASCII als UTF-16LE immer `Byte,
0x00`-Paare und als UTF-32LE/UTF-32BE immer drei `0x00`-Bytes je Zeichen
erzeugt. Das UTF-16LE-BOM (`FF FE`) ist ein reiner Byte-Präfix des
UTF-32LE-BOM (`FF FE 00 00`) — die Erkennung prüft deshalb zuerst die
längeren 4-Byte-Muster, bevor sie auf die kürzeren 2-Byte-Muster zurückfällt.
Das erkannte Encoding wird zusätzlich gegen ein explizites
`text_encoding`-Feld im Header jeder Seite geprüft; bei Widerspruch wird nur
diese eine Seite übersprungen (wie bei jedem anderen Seiten-Header-Fehler).

Praeambel und Header werden **immer als ASCII** gelesen (Nicht-ASCII-Zeichen
gelten dort als unlesbar), unabhängig vom Encoding der Nutzdaten — ein
ungültiges Nutzdaten-Zeichen kann das Lesen des Headers also nicht
verhindern. Ein 8-Bit-Strom, der kein gültiges UTF-8 ist, wird für die
Nutzdaten als **Latin-1** gelesen (mit Hinweis auf stderr). Das betrifft
z.B. `latin128`-Seiten, die ein OCR-Tool oder Editor als „ANSI“ gespeichert
hat; der Header-Cross-Check behandelt das wie UTF-8.

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

## Folgen gleicher Zeichen: Verwürfelung und Längenreparatur

Der häufigste OCR-Fehler bei gut lesbaren Screenshots ist eine falsch
gezählte Folge gleicher Zeichen (`qqqqqqqqqqqq`, `JJJJJJJJJJJJJJJJ`). Ein
Zeichen zu viel oder zu wenig verschiebt den Rest der Zeile, sodass
Reed-Solomon dutzende statt einem Fehler sieht. Zwei Gegenmaßnahmen:

- **Verwürfelung (Encoder, Standard):** Die Datensymbole jeder Zeile werden
  vor dem RS-Kodieren mit einem Pseudo-Zufallsstrom XOR-verknüpft
  (SHAKE-256 aus Dokument-ID und Zeilennummer, siehe `airgap/scramble.py`).
  Dadurch entstehen auch bei komprimierten oder stark repetitiven Daten
  keine langen Folgen mehr. Kostet keinen Platz; `--no-scramble` schaltet
  sie ab (nur für Vergleiche/Debugging). Ab Formatversion 4; der Decoder
  liest Version 3 (unverwürfelt) weiterhin. Formatversion 5 (aktuell)
  erweitert das Zeilen-CRC bei den 512er-Presets auf das volle 9-Bit-Symbol;
  für alle anderen Alphabete sind Version 4 und 5 bit-identisch, ältere
  Dateien bleiben lesbar.
- **Längenreparatur (Decoder, immer aktiv):** Hat eine Zeile bis zu 2
  Zeichen zu viel oder zu wenig (oder ist bei richtiger Länge nicht
  korrigierbar), probiert der Decoder Varianten durch, in denen Folgen
  gleicher Zeichen um ein Zeichen verlängert oder verkürzt sind. Akzeptiert
  wird nur eine Variante, die RS dekodieren kann **und** deren CRC32 stimmt.
  Gerettete Zeilen stehen im Fehlerbericht. Wirkt auch bei älteren
  Version-3-Dateien.

## Format (Kurzfassung)

```
Je Seite (mindestens 1 - ohne --lines besteht die ganze Datei aus einer Seite):
  Zeile 1:         Präambel (5 Zeichen, Header-Alphabet): Anzahl Header-Zeilen + Prüfsumme
  Zeilen 2..N:      Header, 3-fach wiederholt, je mit [Kopie-Idx][Zeilen-Idx][Inhalt]
                    (Formatversion, Flags (verwürfelt), Alphabet, Dokument-ID, Seitenzahl/-nummer/-prüfsumme,
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
  die Nutzdaten ab). Ein OCR-Fehler in der Zeilennummer macht diese eine
  Zeile unbrauchbar, beeinträchtigt aber keine anderen Zeilen: Nummern
  außerhalb des gültigen Bereichs werden verworfen, und kollidiert eine
  falsche Nummer mit einer vorhandenen Zeile, gewinnt die Zeile, deren
  Nachbarn zur Nummer passen (sonst wird die Zeile als widersprüchlich
  gemeldet). Landet eine falsche Nummer ausgerechnet auf einer ohnehin
  fehlenden Zeile, wird sie dort einsortiert — das fällt dann über die
  Seiten-Prüfsumme bzw. SHA-256 auf (Exitcode 4).
- **Geht eine ganze Header-Zeile verloren** (nicht nur eine Kopie davon,
  sondern die Zeile fehlt komplett), verschiebt sich die Grenze zwischen
  Header- und Nutzdatenbereich um eine Zeile. Der Header wird dank der
  übrigen Kopien in der Regel trotzdem rekonstruiert, aber die erste
  Nutzdatenzeile der Seite wird dem Header-Bereich zugeschlagen und im
  Fehlerbericht als fehlend gemeldet (Exitcode 3). Ist der Header gar nicht
  mehr rekonstruierbar, wird nur diese Seite übersprungen. Betrifft nur den
  sehr kleinen Header-Bereich (typischerweise 1-3 Zeilen von insgesamt
  tausenden).
- **RS-Codewort über der Korrekturkapazität**: in extrem seltenen Fällen kann
  eine Zeile mit mehr Fehlern als `floor(r/2)` fälschlich als "korrigierbar"
  erscheinen (siehe `test_rs_codec.py`, dort wird das aktiv gegengetestet;
  in allen bisherigen Testläufen 0 von 20 Fällen je Konfiguration). Das zusätzliche CRC32-Feld fängt
  auch diesen Restfall in der Praxis ab (Kategorie "fragwürdig korrigiert").

## Performance-Hinweis

Reine Python-Implementierung ohne Zusatzpakete, bewusst ohne Parallelisierung
(Batch-Verarbeitung, nicht zeitkritisch). Gemessen mit Python 3.13 auf einem
einzelnen Kern: rund **3 Sekunden pro MB** für Encode und ebenso für Decode
(Default-Einstellungen, `default`- und `utf16le256`-Preset). Die Laufzeit
wächst linear mit der Dateigröße; für 300 MB sind also etwa 15 Minuten je
Richtung zu erwarten. Mehr Parität (`--redundancy`) und viele beschädigte
Zeilen (Längenreparatur probiert zahlreiche Varianten durch) verlängern vor
allem das Decodieren. Vor dem produktiven Einsatz mit dreistelligen MB
empfiehlt sich ein eigener Testlauf mit repräsentativer Dateigröße.

## Tests

Nur Standardbibliothek (`unittest`, pytest funktioniert ebenfalls); alle
Testdateien entstehen in einem temporären Verzeichnis.

```bash
python3 test_rs_codec.py   # RS-Codec: alle Feldgrößen, Korrektur bis exakt floor(r/2), Ablehnung darüber
python3 test_e2e.py        # alle Werkzeuge Ende-zu-Ende (siehe unten)
```

`test_e2e.py` deckt ab: sauberen Roundtrip, korrigierbare Verfälschungen,
verlorene und zusammengefügte Zeilen, Custom-Alphabet und alle
Encoding-Varianten der Presets (UTF-8, UTF-16LE, UTF-32, als „ANSI“
gespeichertes Latin-1), Seitenteilung (gemischte Reihenfolge, Einzeldateien,
Glob-Pattern, fehlende Seite, BOM-Verhalten), Spaltenlayout, `--header`,
`--show-preset`, Verwürfelung, Längenreparatur, leere Dateien, Lesefehler in
Zeilennummern, Exitcodes, Seitenzahl-Obergrenze, `reframe.py` und
`archive2git.py` (letzteres nur, wenn `git` installiert ist).

## Projektstruktur

```
bin/            Startskripte (encode.py, decode.py, reframe.py) und archive2git.py
airgap/         Paket mit der eigentlichen Implementierung
  encode.py, decode.py, reframe.py   Kommandozeilen-Logik (python3 -m airgap.<name>)
  encoder.py    gemeinsame Encode-Pipeline (Layout, Zeilen, Seiten, Ausgabe)
  framing.py    Aufbau/Parsen einer Nutzdatenzeile, Layout, Längenreparatur, Spalten
  header.py     Header-Format, Präambel, 3-fach-Kopien mit Mehrheitsentscheid
  rs_codec.py   Reed-Solomon über GF(2^5) bis GF(2^9)
  alphabets.py  Header-Alphabet, Presets, Bit-Slicing Bytes <-> Symbole
  scramble.py   Verwürfelung der Nutzdatensymbole
  progress.py   Fortschrittsbalken
  util.py       Ganzzahl-Hilfsfunktionen
test_rs_codec.py, test_e2e.py
```
