#!/usr/bin/env python3
"""
Decoder: OCR-Text -> Datei(en) + Fehlerbericht.

Beispiel:
    python3 decode.py ocr_output.txt -o wiederhergestellt
    -> erzeugt entweder
         wiederhergestellt              (wenn alles fehlerfrei rekonstruiert wurde)
       oder bei Fehlern/Luecken:
         wiederhergestellt.part1        (sauberer Anfangsteil)
         wiederhergestellt.part2        (Rest, inkl. Luecken als Nullbytes)
         wiederhergestellt.errors.txt   (Fehlerbericht, Format via --error-report-format)

Format-/Versionsfehler und ein nicht rekonstruierbarer Header fuehren zu einem
harten Abbruch (siehe README.md) - hier wird bewusst NICHT versucht, "irgendwas"
auszugeben.
"""
import argparse
import hashlib
import json
import os
import sys

# lib/ relativ zu diesem Skript einbinden, unabhaengig vom Aufrufverzeichnis
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from alphabets import Alphabet, DEFAULT_ALPHABET, HEADER_ALPHABET
from header import (
    HeaderError, parse_preamble, parse_header_lines, unpack_header, HEADER_COPIES,
)
from rs_codec import GaloisField, rs_generator_poly
from framing import parse_payload_line, crc_width_for, reflow_joined_lines, LineStatus


def main():
    ap = argparse.ArgumentParser(description="Air-Gap Text-Decoder mit Reed-Solomon-Fehlerkorrektur")
    ap.add_argument("input", nargs="?", help="OCR-Textdatei (Default: stdin)")
    ap.add_argument("-o", "--output", default="output", help="Basisname der Ausgabedatei(en) (Default: 'output')")
    ap.add_argument("--error-report-format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    if args.input:
        with open(args.input, "r", encoding="utf-8", errors="strict") as f:
            raw_lines = f.read().split("\n")
    else:
        sys.stdin.reconfigure(encoding="utf-8")
        raw_lines = sys.stdin.read().split("\n")

    # Fuehrende/nachgestellte Leerzeilen durch OCR-Artefakte ignorieren,
    # aber die erste inhaltstragende Zeile bleibt die Praeambel.
    lines = [l for l in raw_lines]
    non_empty_idx = [i for i, l in enumerate(lines) if l.strip() != ""]
    if not non_empty_idx:
        print("FEHLER: Eingabe ist komplett leer.", file=sys.stderr)
        sys.exit(1)

    header_alpha = Alphabet(HEADER_ALPHABET)
    preamble_idx = non_empty_idx[0]
    preamble_line = lines[preamble_idx].strip()

    try:
        n_header_lines = parse_preamble(preamble_line, header_alpha)
    except HeaderError as e:
        print(f"FEHLER (Praeambel): {e}", file=sys.stderr)
        sys.exit(1)

    expected_header_body = n_header_lines * HEADER_COPIES
    body_window = lines[preamble_idx + 1: preamble_idx + 1 + expected_header_body]
    rest_lines = lines[preamble_idx + 1 + expected_header_body:]

    try:
        header_bytes = parse_header_lines(preamble_line, body_window, 0, header_alpha)
        meta = unpack_header(header_bytes)
    except HeaderError as e:
        print(f"FEHLER (Header nicht rekonstruierbar): {e}", file=sys.stderr)
        sys.exit(1)

    payload_alpha = Alphabet(meta["alphabet_chars"]) if meta["is_custom_alphabet"] else Alphabet(DEFAULT_ALPHABET)
    gf = GaloisField(payload_alpha.bits_per_symbol)
    gen = rs_generator_poly(meta["r"], gf)
    crc_width = crc_width_for(header_alpha)
    k, r, ln_width = meta["k"], meta["r"], meta["ln_width"]

    # Clipboard-Transfer (statt OCR) bei grossen Dateien braucht mehrere
    # Copy/Paste-Vorgaenge; an der Nahtstelle geht dabei gelegentlich ein
    # Zeilenumbruch verloren, wodurch zwei oder mehr Nutzdatenzeilen zu einer
    # Roh-Zeile verschmelzen. Da jede Nutzdatenzeile eine bekannte, feste
    # Laenge hat, laesst sich das erkennen und automatisch rueckgaengig machen.
    payload_line_width = ln_width + crc_width + k + r
    rest_lines, n_rejoined = reflow_joined_lines(rest_lines, payload_line_width)
    if n_rejoined:
        print(
            f"Hinweis: {n_rejoined} Roh-Zeile(n) enthielten offenbar mehrere zusammengefuegte "
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
        total_data_symbols = -(-(meta["total_file_size"] * 8) // payload_alpha.bits_per_symbol)  # ceil
        total_lines = max(1, -(-total_data_symbols // k))  # ceil
    else:
        total_lines = None  # wird nach dem Scan gesetzt

    results = {}  # line_number -> parse_payload_line()-Ergebnis
    parse_errors = []  # Zeilen, die sich nicht mal als Payload-Zeile lesen liessen (keine line_number)

    # Monotonie-Pruefung: Zeilennummern muessen streng steigen.
    # Taucht eine Nummer m <= zuletzt gesehener Nummer n auf, wird sie
    # stillschweigend verworfen. Das behebt Artefakte beim Zusammenfuegen
    # mehrerer Screenshots (Stitching), bei denen ueberlappende Randbereiche
    # zu wiederholten oder rueckwaerts laufenden Zeilennummern fuehren.
    max_seen_ln = -1

    for raw in rest_lines:
        res = parse_payload_line(raw, k, r, gen, gf, ln_width, crc_width, payload_alpha, header_alpha)
        if res is None:
            continue  # komplett leere Zeile -> ignorieren, zaehlt spaeter als Luecke
        ln = res["line_number"]
        if ln is None:
            parse_errors.append(res)
            continue
        if ln <= max_seen_ln:
            continue  # Stitching-Artefakt: stillschweigend verwerfen
        max_seen_ln = ln
        # bei Duplikaten: 'ok' hat Vorrang vor allem anderen
        if ln not in results or (results[ln]["status"] != LineStatus.OK and res["status"] == LineStatus.OK):
            results[ln] = res

    # total_lines bei total_file_size==0 jetzt aus Scan-Ergebnis ableiten
    if total_lines is None:
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

    bits_per_symbol = payload_alpha.bits_per_symbol

    def byte_offset_of_line(line_no: int) -> int:
        return (line_no * k * bits_per_symbol) // 8

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
                     filename=meta["filename"])
        print(f"OK: vollstaendig rekonstruiert -> {args.output} (Hash-Pruefung: {hash_status})", file=sys.stderr)
    else:
        split_at = byte_offset_of_line(first_bad_line)
        part1 = full_bytes[:split_at]
        part2 = full_bytes[split_at:]
        p1_path, p2_path = f"{args.output}.part1", f"{args.output}.part2"
        with open(p1_path, "wb") as f:
            f.write(part1)
        with open(p2_path, "wb") as f:
            f.write(part2)
        write_report(args, status="unvollstaendig (Hash-Pruefung uebersprungen)", hash_checked=False, hash_ok=None,
                     bad_lines=bad_lines, parse_errors=parse_errors, total_lines=total_lines,
                     filename=meta["filename"])
        print(
            f"UNVOLLSTAENDIG: {len(bad_lines)} von {total_lines} Zeilen fehlerhaft/fehlend.\n"
            f"  {p1_path} (sauberer Anfang, {len(part1)} Bytes)\n"
            f"  {p2_path} (Rest inkl. Luecken, {len(part2)} Bytes)\n"
            f"  {args.output}.errors.{ 'json' if args.error_report_format=='json' else 'txt'} (Fehlerbericht)",
            file=sys.stderr,
        )


def write_report(args, status, hash_checked, hash_ok, bad_lines, parse_errors, total_lines, filename):
    ext = "json" if args.error_report_format == "json" else "txt"
    path = f"{args.output}.errors.{ext}"
    if args.error_report_format == "json":
        payload = dict(
            status=status, filename=filename, total_lines=total_lines,
            hash_checked=hash_checked, hash_ok=hash_ok,
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
            if hash_checked:
                f.write(f"SHA-256-Pruefung: {'OK' if hash_ok else 'FEHLGESCHLAGEN'}\n")
            else:
                f.write("SHA-256-Pruefung: uebersprungen (unvollstaendig)\n")
            f.write(f"\nFehlerhafte/fehlende Zeilen ({len(bad_lines)}):\n")
            for ln, st, reason in bad_lines:
                f.write(f"  Zeile {ln}: {st} - {reason}\n")
            if parse_errors:
                f.write(f"\nNicht zuordenbare Zeilen ({len(parse_errors)}):\n")
                for r in parse_errors:
                    f.write(f"  {r['status']} - {r['reason']}\n")


if __name__ == "__main__":
    main()
