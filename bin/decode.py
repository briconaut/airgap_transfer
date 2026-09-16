#!/usr/bin/env python3
"""
Decoder: OCR-Text/Seiten -> Datei(en) + Fehlerbericht.

Beispiel:
    python3 decode.py ocr_output.txt -o wiederhergestellt
    python3 decode.py seite1.txt seite2.txt seite3.txt -o wiederhergestellt
    python3 decode.py "seite_*.txt" -o wiederhergestellt
    -> erzeugt entweder
         wiederhergestellt              (wenn alles fehlerfrei rekonstruiert wurde)
       oder bei Fehlern/Luecken:
         wiederhergestellt.part1        (sauberer Anfangsteil)
         wiederhergestellt.part2        (Rest, inkl. nicht rekonstruierbarer
                                          Stellen als Nullbytes)
         wiederhergestellt.errors.txt   (Fehlerbericht, Format via --error-report-format)

Eingabe kann aus einer einzigen Datei/stdin (ggf. mit mehreren Seiten
hintereinander, in beliebiger Reihenfolge), mehreren einzelnen Dateien oder
einem Glob-Pattern bestehen — decode.py sucht ueberall im kombinierten
Zeilenstrom nach Seiten-Praeambeln und setzt die Seiten anhand ihrer
Zeilennummern zusammen, unabhaengig von der Reihenfolge der Eingabe.

UTF-8 vs. UTF-16LE (siehe --preset utf16le128/utf16le256 in encode.py) wird
PRO QUELLE automatisch anhand der Rohbytes erkannt, ohne die Datei neu zu
oeffnen: das Header-Alphabet ist immer reines 7-Bit-ASCII und 0x00 ist in
jedem Alphabet verboten, also kann UTF-8 nie ein rohes 0x00-Byte enthalten,
waehrend ASCII als UTF-16LE immer "Byte, 0x00"-Paare erzeugt - eindeutig
erkennbar an den ersten paar Bytes, auch ohne BOM (wichtig, wenn eine
Seite als eigene Datei ohne den fuehrenden BOM der Gesamtdatei vorliegt).

Formatfehler (falsche Versionsnummer) fuehren zu einem harten Abbruch. Der
Header EINER Seite ist weiterhin nicht redundant abgesichert (siehe
README.md) - ist er nicht lesbar, wird NUR diese Seite uebersprungen; erst
wenn KEINE einzige Seite einen lesbaren Header liefert, bricht das Tool
komplett ab (kein Best-Effort-Rateversuch ohne jede header-Information).
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import zlib

# lib/ relativ zu diesem Skript einbinden, unabhaengig vom Aufrufverzeichnis
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from alphabets import Alphabet, DEFAULT_ALPHABET, HEADER_ALPHABET
from header import (
    HeaderError, parse_preamble, parse_header_lines, unpack_header, HEADER_COPIES,
    find_preamble_positions, TEXT_ENCODING_UTF16LE,
)
from rs_codec import GaloisField, rs_generator_poly
from framing import (
    parse_payload_line, crc_width_for, reflow_joined_lines, byte_offset_of_line, LineStatus,
)


def gather_input_paths(input_args):
    """input_args: Liste von Pfaden/Glob-Patterns. Expandiert Wildcards, bricht
    hart ab wenn ein Pattern auf keine Datei passt (Tippfehler-Schutz)."""
    paths = []
    for tok in input_args:
        if any(c in tok for c in "*?["):
            matches = sorted(glob.glob(tok))
            if not matches:
                print(f"FEHLER: Pattern {tok!r} passt auf keine Datei.", file=sys.stderr)
                sys.exit(1)
            paths.extend(matches)
        else:
            paths.append(tok)
    return paths


def sniff_and_decode(raw: bytes):
    """Erkennt UTF-8 vs. UTF-16LE anhand der Rohbytes und decodiert damit -
    kein erneutes Oeffnen/Lesen noetig (siehe Modul-Docstring). Ein
    fuehrendes UTF-16LE-BOM (FF FE) wird erkannt und uebersprungen, ist aber
    fuer die Erkennung selbst NICHT erforderlich: da 0x00 in jedem Alphabet
    verboten ist (siehe Alphabet.__init__ in alphabets.py) und das Header-
    Alphabet immer reines 7-Bit-ASCII ist, kann ein UTF-8-Strom nie ein
    rohes 0x00-Byte enthalten, waehrend ASCII als UTF-16LE IMMER 'Byte,
    0x00'-Paare erzeugt - eindeutig unterscheidbar an den ersten Bytes.
    Rueckgabe: (text, encoding_name)."""
    if raw[:2] == b"\xff\xfe":
        text, encoding = raw[2:].decode("utf-16-le"), "utf-16-le"
    elif len(raw) >= 2 and raw[1] == 0:
        text, encoding = raw.decode("utf-16-le"), "utf-16-le"
    else:
        text, encoding = raw.decode("utf-8"), "utf-8"
    # Binäres Lesen umgeht Pythons Universal-Newlines-Übersetzung (die ein
    # Text-mode open() automatisch macht) - von Hand nachholen, falls die
    # Quelle mit \r\n oder \r statt \n endet (z.B. unter Windows erzeugt).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, encoding


def read_all_input_lines(input_args):
    """Liest alle Quellen (stdin, oder eine/mehrere Dateien/Patterns) als
    Rohbytes, erkennt und decodiert jede Quelle EINZELN (sniff_and_decode)
    und haengt die resultierenden Zeilenlisten aneinander - sonst koennte
    die letzte Zeile einer Datei mit der ersten Zeile der naechsten
    verschmelzen.

    Rueckgabe: (lines, source_ranges). source_ranges: Liste von
    (start_idx, end_idx_exklusiv, encoding_name) pro Quelle, fuer den
    spaeteren Cross-Check gegen das von jeder Seite im Header deklarierte
    text_encoding-Feld (siehe main())."""
    if not input_args:
        sources = [("<stdin>", sys.stdin.buffer.read())]
    else:
        sources = []
        for path in gather_input_paths(input_args):
            with open(path, "rb") as f:
                sources.append((path, f.read()))

    lines = []
    source_ranges = []
    for name, raw in sources:
        try:
            text, encoding = sniff_and_decode(raw)
        except UnicodeDecodeError as e:
            print(f"FEHLER: {name} laesst sich weder als UTF-8 noch als UTF-16LE lesen ({e}).", file=sys.stderr)
            sys.exit(1)
        start = len(lines)
        lines.extend(text.split("\n"))
        source_ranges.append((start, len(lines), encoding))
    return lines, source_ranges


def encoding_at(idx, source_ranges):
    for start, end, encoding in source_ranges:
        if start <= idx < end:
            return encoding
    return "utf-8"  # sollte nie vorkommen - idx stammt immer aus einem der Bereiche


def main():
    ap = argparse.ArgumentParser(description="Air-Gap Text-Decoder mit Reed-Solomon-Fehlerkorrektur")
    ap.add_argument(
        "input", nargs="*",
        help=(
            "OCR-Text-/Seiten-Datei(en) (Default: stdin). Mehrere Dateien und/oder "
            "Glob-Patterns (z.B. 'seite_*.txt') sind erlaubt - alle werden zusammen "
            "nach Seiten durchsucht, unabhaengig von der Reihenfolge."
        ),
    )
    ap.add_argument("-o", "--output", default="output", help="Basisname der Ausgabedatei(en) (Default: 'output')")
    ap.add_argument("--error-report-format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    lines, source_ranges = read_all_input_lines(args.input)
    if not any(l.strip() for l in lines):
        print("FEHLER: Eingabe ist komplett leer.", file=sys.stderr)
        sys.exit(1)

    header_alpha = Alphabet(HEADER_ALPHABET)

    preamble_positions = find_preamble_positions(lines, header_alpha)
    if not preamble_positions:
        print("FEHLER (Praeambel): keine gueltige Seiten-Praeambel in der Eingabe gefunden.", file=sys.stderr)
        sys.exit(1)

    # --- Pass 1: jede Seite fuer sich parsen (Header-Fehler sind hier NICHT
    # fatal fuer den ganzen Lauf - nur diese eine Seite faellt aus) -----------
    pages = []  # dicts: meta, rest_lines
    for i, start in enumerate(preamble_positions):
        end = preamble_positions[i + 1] if i + 1 < len(preamble_positions) else len(lines)
        seg = lines[start:end]
        preamble_line = seg[0].strip()
        try:
            n_header_lines = parse_preamble(preamble_line, header_alpha)
            expected_header_body = n_header_lines * HEADER_COPIES
            body_window = seg[1:1 + expected_header_body]
            seg_rest_lines = seg[1 + expected_header_body:]
            header_bytes = parse_header_lines(preamble_line, body_window, 0, header_alpha)
            meta = unpack_header(header_bytes)
        except HeaderError as e:
            print(f"Hinweis: Header einer Seite nicht rekonstruierbar ({e}) - diese Seite wird uebersprungen.", file=sys.stderr)
            continue

        # Cross-Check: von dieser Seite deklariertes text_encoding-Feld muss
        # zum tatsaechlich per Byte-Sniffing erkannten Encoding ihrer Quelle
        # passen (siehe read_all_input_lines/sniff_and_decode). Nicht fatal
        # fuer den ganzen Lauf - wie jeder andere Seiten-Header-Fehler auch.
        declared_encoding = "utf-16-le" if meta["text_encoding"] == TEXT_ENCODING_UTF16LE else "utf-8"
        sniffed_encoding = encoding_at(start, source_ranges)
        if declared_encoding != sniffed_encoding:
            print(
                f"Hinweis: Seite deklariert Encoding {declared_encoding!r}, wurde aber als "
                f"{sniffed_encoding!r} gelesen - diese Seite wird uebersprungen.",
                file=sys.stderr,
            )
            continue

        pages.append(dict(meta=meta, rest_lines=seg_rest_lines))

    if not pages:
        print("FEHLER (Header nicht rekonstruierbar): keine einzige Seite hatte einen lesbaren Header.", file=sys.stderr)
        sys.exit(1)

    # --- Dokument-ID pruefen: unterschiedliche Dokumente nicht stillschweigend mischen
    doc_ids = {p["meta"]["document_id"] for p in pages}
    if len(doc_ids) > 1:
        ids_hex = ", ".join(sorted(d.hex() for d in doc_ids))
        print(
            f"FEHLER: Seiten mit unterschiedlichen Dokument-IDs in der Eingabe gemischt "
            f"({ids_hex}) - Abbruch, kein automatisches Aufteilen auf mehrere Dokumente.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Gemeinsame Felder: identisch auf jeder Seite, also reicht eine beliebige.
    meta = pages[0]["meta"]
    payload_alpha = Alphabet(meta["alphabet_chars"]) if meta["is_custom_alphabet"] else Alphabet(DEFAULT_ALPHABET)
    gf = GaloisField(payload_alpha.bits_per_symbol)
    gen = rs_generator_poly(meta["r"], gf)
    crc_width = crc_width_for(header_alpha)
    k, r, ln_width = meta["k"], meta["r"], meta["ln_width"]
    bits_per_symbol = payload_alpha.bits_per_symbol
    page_count = meta["page_count"]

    seen_page_numbers = sorted(p["meta"]["page_number"] for p in pages)
    if page_count > 1 or len(pages) > 1:
        print(f"Seiten gelesen: {len(pages)} von {page_count} (Nummern: {seen_page_numbers}).", file=sys.stderr)

    # Clipboard-Transfer (statt OCR) bei grossen Dateien braucht mehrere
    # Copy/Paste-Vorgaenge; an der Nahtstelle geht dabei gelegentlich ein
    # Zeilenumbruch verloren, wodurch zwei oder mehr Nutzdatenzeilen zu einer
    # Roh-Zeile verschmelzen. Da jede Nutzdatenzeile eine bekannte, feste
    # Laenge hat, laesst sich das erkennen und automatisch rueckgaengig machen.
    payload_line_width = ln_width + crc_width + k + r

    # --- Pass 2: Nutzdatenzeilen jeder Seite parsen, in gemeinsames Ergebnis --
    results = {}  # line_number -> parse_payload_line()-Ergebnis (ueber ALLE Seiten)
    parse_errors = []
    page_line_ranges = {}  # page_number -> (min_ln, max_ln) der tatsaechlich gefundenen Zeilen dieser Seite
    total_rejoined = 0

    for p in pages:
        seg_rest_lines, n_rejoined = reflow_joined_lines(p["rest_lines"], payload_line_width)
        total_rejoined += n_rejoined

        # Monotonie-Pruefung PRO SEITE (nicht mehr global - Seiten koennen in
        # beliebiger Reihenfolge vorliegen): Zeilennummern muessen innerhalb
        # einer Seite streng steigen. Taucht eine Nummer m <= zuletzt gesehener
        # Nummer n auf, wird sie stillschweigend verworfen (Stitching-Artefakt
        # bei ueberlappenden Screenshots derselben Seite).
        max_seen_ln = -1
        page_lns = []
        for raw in seg_rest_lines:
            res = parse_payload_line(raw, k, r, gen, gf, ln_width, crc_width, payload_alpha, header_alpha)
            if res is None:
                continue  # komplett leere Zeile -> ignorieren
            ln = res["line_number"]
            if ln is None:
                parse_errors.append(res)
                continue
            page_lns.append(ln)
            if ln <= max_seen_ln:
                continue
            max_seen_ln = ln
            # bei Duplikaten (auch ueber Seiten hinweg, z.B. doppelt eingefuegte
            # Seite): 'ok' hat Vorrang vor allem anderen
            if ln not in results or (results[ln]["status"] != LineStatus.OK and res["status"] == LineStatus.OK):
                results[ln] = res
        if page_lns:
            page_line_ranges[p["meta"]["page_number"]] = (min(page_lns), max(page_lns))

    if total_rejoined:
        print(
            f"Hinweis: {total_rejoined} Roh-Zeile(n) enthielten offenbar mehrere zusammengefuegte "
            f"Nutzdatenzeilen (Zeilenumbruch-Verlust, z.B. Zwischenablage-Artefakt) und wurden "
            f"anhand der bekannten Zeilenbreite ({payload_line_width} Symbole) automatisch wieder "
            f"aufgeteilt.",
            file=sys.stderr,
        )

    # total_lines aus dem Header ableiten — ausser bei total_file_size==0
    # (gesetzt von reframe.py, das die Originallaenge nicht kennt). In diesem
    # Fall wird total_lines nach dem Scan aus der hoechsten gesehenen
    # Zeilennummer ermittelt.
    if meta["total_file_size"] > 0:
        total_data_symbols = -(-(meta["total_file_size"] * 8) // bits_per_symbol)  # ceil
        total_lines = max(1, -(-total_data_symbols // k))  # ceil
    else:
        total_lines = max(results.keys()) + 1 if results else 1

    # --- Vollstaendigen Symbolstrom rekonstruieren (Luecken = 0-Symbole) ----
    full_symbols = [0] * (total_lines * k)
    bad_lines = []  # (line_number, status, reason)
    first_bad_line = None
    for i in range(total_lines):
        res = results.get(i)
        if res is None:
            bad_lines.append((i, "fehlend", "Zeile in der Eingabe nicht gefunden"))
            if first_bad_line is None:
                first_bad_line = i
            continue
        if res["status"] == LineStatus.OK:
            full_symbols[i * k:(i + 1) * k] = res["data"]
        else:
            bad_lines.append((i, res["status"], res["reason"]))
            if first_bad_line is None:
                first_bad_line = i
            # 'fragwuerdig_korrigiert' Daten NICHT in den Hauptstrom uebernehmen,
            # aber fuer eine manuelle Nachpruefung im Fehlerbericht referenzieren.

    # expected_length=None -> Floor-Trick (fuer reframe.py-Output, der total_file_size=0 setzt).
    # expected_length=N    -> harter Schnitt auf N Bytes (normaler encode.py-Output).
    expected_length = meta["total_file_size"] if meta["total_file_size"] > 0 else None
    full_bytes = payload_alpha.symbols_to_bytes(full_symbols, expected_length=expected_length)

    # --- Seiten-Pruefsummen verifizieren (zusaetzlich zu CRC/RS pro Zeile) ---
    # Byte-Bereich einer Seite wird EMPIRISCH aus den tatsaechlich auf dieser
    # Seite gefundenen Zeilennummern abgeleitet (kein eigenes "Zeilen pro
    # Seite"-Feld noetig). Fehlt die erste/letzte Zeile einer Seite, wird der
    # Bereich etwas zu eng geschaetzt - die Pruefsumme schlaegt dann zwar an,
    # aber das ist redundant zur ohnehin schon erkannten fehlenden Zeile, nie
    # ein falsches "OK".
    missing_pages = sorted(set(range(page_count)) - set(seen_page_numbers))
    bad_pages = []
    for p in pages:
        pn = p["meta"]["page_number"]
        rng = page_line_ranges.get(pn)
        if rng is None:
            bad_pages.append((pn, "keine gueltige Nutzdatenzeile auf dieser Seite gefunden"))
            continue
        min_ln, max_ln = rng
        b0 = byte_offset_of_line(min_ln, k, bits_per_symbol)
        b1 = min(byte_offset_of_line(max_ln + 1, k, bits_per_symbol), len(full_bytes))
        actual_crc = zlib.crc32(full_bytes[b0:b1]) & 0xFFFFFFFF
        if actual_crc != p["meta"]["page_checksum"]:
            bad_pages.append((pn, "Seiten-Pruefsumme stimmt nicht (Seite vermutlich unvollstaendig/beschaedigt)"))

    if first_bad_line is None:
        # Alles sauber -> ein einziges vollstaendiges Ausgabefile.
        # Hash-Pruefung nur, wenn SHA-256 im Header nicht Null-Bytes sind
        # (Null-Bytes = Sentinel von reframe.py: keine Originalreferenz vorhanden).
        null_digest = bytes(32)
        if meta["sha256"] != null_digest:
            digest = hashlib.sha256(full_bytes).digest()
            hash_ok = digest == meta["sha256"]
            hash_checked = True
        else:
            hash_ok = None
            hash_checked = False
        with open(args.output, "wb") as f:
            f.write(full_bytes)
        hash_status = "OK" if hash_ok else ("FEHLGESCHLAGEN" if hash_ok is not None else "uebersprungen (kein Hash im Header)")
        write_report(args, status="vollstaendig", hash_checked=hash_checked, hash_ok=hash_ok,
                     bad_lines=[], parse_errors=parse_errors, total_lines=total_lines,
                     filename=meta["filename"], page_count=page_count,
                     missing_pages=missing_pages, bad_pages=bad_pages)
        print(f"OK: vollstaendig rekonstruiert -> {args.output} (Hash-Pruefung: {hash_status})", file=sys.stderr)
    else:
        split_at = byte_offset_of_line(first_bad_line, k, bits_per_symbol)
        part1 = full_bytes[:split_at]
        part2 = full_bytes[split_at:]
        p1_path, p2_path = f"{args.output}.part1", f"{args.output}.part2"
        with open(p1_path, "wb") as f:
            f.write(part1)
        with open(p2_path, "wb") as f:
            f.write(part2)
        write_report(args, status="unvollstaendig (Hash-Pruefung uebersprungen)", hash_checked=False, hash_ok=None,
                     bad_lines=bad_lines, parse_errors=parse_errors, total_lines=total_lines,
                     filename=meta["filename"], page_count=page_count,
                     missing_pages=missing_pages, bad_pages=bad_pages)
        page_note = f", {len(missing_pages)} Seite(n) komplett fehlend" if missing_pages else ""
        print(
            f"UNVOLLSTAENDIG: {len(bad_lines)} von {total_lines} Zeilen fehlerhaft/fehlend{page_note}.\n"
            f"  {p1_path} (sauberer Anfang, {len(part1)} Bytes)\n"
            f"  {p2_path} (Rest inkl. Luecken, {len(part2)} Bytes)\n"
            f"  {args.output}.errors.{ 'json' if args.error_report_format=='json' else 'txt'} (Fehlerbericht)",
            file=sys.stderr,
        )


def write_report(args, status, hash_checked, hash_ok, bad_lines, parse_errors, total_lines, filename,
                  page_count, missing_pages, bad_pages):
    ext = "json" if args.error_report_format == "json" else "txt"
    path = f"{args.output}.errors.{ext}"
    if args.error_report_format == "json":
        payload = dict(
            status=status, filename=filename, total_lines=total_lines,
            hash_checked=hash_checked, hash_ok=hash_ok,
            page_count=page_count, missing_pages=missing_pages,
            bad_pages=[dict(page=pn, reason=reason) for pn, reason in bad_pages],
            bad_lines=[dict(line=ln, status=st, reason=reason) for ln, st, reason in bad_lines],
            unparseable_lines=[dict(status=r["status"], reason=r["reason"]) for r in parse_errors],
        )
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    else:
        with open(path, "w") as f:
            f.write(f"Status: {status}\n")
            f.write(f"Dateiname (aus Header): {filename or '(keiner gespeichert)'}\n")
            f.write(f"Zeilen gesamt: {total_lines}\n")
            f.write(f"Seiten gesamt: {page_count}\n")
            if hash_checked:
                f.write(f"SHA-256-Pruefung: {'OK' if hash_ok else 'FEHLGESCHLAGEN'}\n")
            else:
                f.write("SHA-256-Pruefung: uebersprungen (unvollstaendig)\n")
            if missing_pages or bad_pages:
                f.write(f"\nSeiten mit Problemen:\n")
                for pn in missing_pages:
                    f.write(f"  Seite {pn}: komplett fehlend (kein lesbarer Header gefunden)\n")
                for pn, reason in bad_pages:
                    f.write(f"  Seite {pn}: {reason}\n")
            f.write(f"\nFehlerhafte/fehlende Zeilen ({len(bad_lines)}):\n")
            for ln, st, reason in bad_lines:
                f.write(f"  Zeile {ln}: {st} - {reason}\n")
            if parse_errors:
                f.write(f"\nNicht zuordenbare Zeilen ({len(parse_errors)}):\n")
                for r in parse_errors:
                    f.write(f"  {r['status']} - {r['reason']}\n")


if __name__ == "__main__":
    main()
