"""End-to-End-Tests fuer encode.py/decode.py/reframe.py/archive2git.py.

Ausfuehren mit 'python3 test_e2e.py' (unittest) oder pytest. Alle Dateien
landen in einem temporaeren Verzeichnis, das am Ende geloescht wird."""
import atexit
import base64
import glob
import os
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from airgap.alphabets import Alphabet, DEFAULT_ALPHABET
from airgap.framing import symbols_crc32
from airgap.header import find_preamble_positions, parse_preamble, HEADER_COPIES

# Pfade zu den Tools relativ zu diesem Skript
ENCODE  = os.path.join(HERE, "bin", "encode.py")
DECODE  = os.path.join(HERE, "bin", "decode.py")
REFRAME = os.path.join(HERE, "bin", "reframe.py")
ARCHIVE2GIT = os.path.join(HERE, "bin", "archive2git.py")

# Exitcodes von decode.py (siehe airgap/decode.py)
EXIT_INCOMPLETE = 3
EXIT_HASH_MISMATCH = 4

random.seed(7)
WORKDIR = tempfile.mkdtemp(prefix="airgap_e2e_")
atexit.register(shutil.rmtree, WORKDIR, ignore_errors=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r


def make_test_file(path, size):
    with open(path, "wb") as f:
        f.write(bytes(random.getrandbits(8) for _ in range(size)))


def split_pages(lines):
    """Zerlegt eine Zeilenliste (encode.py --lines-Output) an den Seiten-
    Praeambeln in ihre einzelnen Seiten (jede Seite: Liste von Zeilen,
    beginnend mit ihrer eigenen Praeambel)."""
    header_alpha = Alphabet(DEFAULT_ALPHABET)
    positions = find_preamble_positions(lines, header_alpha)
    pages = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(lines)
        pages.append(lines[start:end])
    return pages


def test_clean_roundtrip():
    print("== Test 1: sauberer Roundtrip ==")
    src = f"{WORKDIR}/t1_src.bin"
    enc = f"{WORKDIR}/t1_enc.txt"
    out = f"{WORKDIR}/t1_out"
    make_test_file(src, 20_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20"])
    assert r.returncode == 0, r.stderr
    print(" encode stderr:", r.stderr.strip().splitlines()[-1] if r.stderr else "")

    r = run([sys.executable, DECODE, enc, "-o", out])
    assert r.returncode == 0, r.stderr
    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual, "Roundtrip-Inhalt stimmt nicht ueberein!"
    print(" OK: Datei exakt rekonstruiert,", len(actual), "Bytes")


def corrupt_symbols_in_line(line, n, protect_prefix, alphabet_chars):
    """Ersetzt n Zeichen (nur im Payload-Teil, ab protect_prefix) durch ein ANDERES
    Zeichen desselben Alphabets - simuliert einen reinen Symbolfehler, den RS
    prinzipiell korrigieren kann (ein Zeichen ausserhalb des Alphabets waere
    dagegen ein Lesefehler, den wir separat testen wollen, nicht hier)."""
    chars = list(line)
    positions = random.sample(range(protect_prefix, len(chars)), min(n, len(chars) - protect_prefix))
    for p in positions:
        old = chars[p]
        new = old
        while new == old:
            new = random.choice(alphabet_chars)
        chars[p] = new
    return "".join(chars)


def test_correctable_corruption():
    print("== Test 2: korrigierbare Verfaelschung (innerhalb RS-Kapazitaet) ==")
    src = f"{WORKDIR}/t2_src.bin"
    enc = f"{WORKDIR}/t2_enc.txt"
    corrupted = f"{WORKDIR}/t2_corrupt.txt"
    out = f"{WORKDIR}/t2_out"
    make_test_file(src, 15_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "30"])
    assert r.returncode == 0, r.stderr

    with open(enc) as f:
        lines = f.read().split("\n")

    # r ermitteln: aus stderr-Ausgabe des Encoders extrahieren
    stderr_txt = r.stderr
    r_val = int(stderr_txt.split("r=")[1].split(" ")[0])
    max_corr = r_val // 2
    print(f" r={r_val}, max_correctable={max_corr} Fehler/Zeile")

    # ein paar Nutzdatenzeilen (nicht Header/Praeambel) leicht verfaelschen,
    # aber innerhalb der Korrekturfaehigkeit bleiben
    data_line_indices = [i for i, l in enumerate(lines) if l.strip()][-10:]  # letzte 10 nicht-leeren Zeilen (sicher Payload)
    # Praefix (Zeilennummer+CRC, Header-Alphabet) unangetastet lassen - wir wollen
    # gezielt reine Nutzdaten-Symbolfehler testen, keine Praefix-Verwechslungen.
    for idx in random.sample(data_line_indices, 3):
        lines[idx] = corrupt_symbols_in_line(lines[idx], max_corr, protect_prefix=10, alphabet_chars=DEFAULT_ALPHABET)

    with open(corrupted, "w") as f:
        f.write("\n".join(lines))

    r2 = run([sys.executable, DECODE, corrupted, "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual, "Nach Korrektur stimmt die Datei nicht ueberein!"
    print(" OK: trotz Verfaelschung exakt rekonstruiert")


def test_lost_lines():
    print("== Test 3: komplett verlorene Zeilen (simuliert OCR-Verlust) ==")
    src = f"{WORKDIR}/t3_src.bin"
    enc = f"{WORKDIR}/t3_enc.txt"
    damaged = f"{WORKDIR}/t3_damaged.txt"
    out = f"{WORKDIR}/t3_out"
    make_test_file(src, 15_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20"])
    assert r.returncode == 0, r.stderr

    with open(enc) as f:
        lines = f.read().split("\n")
    non_empty = [i for i, l in enumerate(lines) if l.strip()]
    # eine Zeile relativ weit hinten in den Nutzdaten komplett entfernen (nicht Header/Praeambel!)
    victim = non_empty[-5]
    del lines[victim]

    with open(damaged, "w") as f:
        f.write("\n".join(lines))

    r2 = run([sys.executable, DECODE, damaged, "-o", out])
    assert r2.returncode == EXIT_INCOMPLETE, r2.stderr
    print(" decode stderr:\n", r2.stderr)

    assert os.path.exists(f"{out}.part1") and os.path.exists(f"{out}.part2"), "part1/part2 wurden nicht erzeugt!"
    with open(f"{out}.part1", "rb") as f:
        part1 = f.read()
    with open(src, "rb") as f:
        expected = f.read()
    assert expected.startswith(part1), "part1 ist kein korrekter Praefix der Originaldatei!"
    print(f" OK: part1 ({len(part1)} Bytes) ist ein korrekter Praefix; Fehlerbericht wurde erzeugt")
    with open(f"{out}.errors.txt") as f:
        print(" --- Fehlerbericht ---")
        print(f.read())


def test_custom_alphabet():
    print("== Test 4: Custom-Alphabet (128 Symbole) ==")
    src = f"{WORKDIR}/t4_src.bin"
    enc = f"{WORKDIR}/t4_enc.txt"
    out = f"{WORKDIR}/t4_out"
    make_test_file(src, 8_000)

    # Demo eines Custom-Alphabets, das bewusst ueber 7-Bit-ASCII hinausgeht
    # (simuliert ein bestaetigtes Terminal mit erweiterter Zeichendarstellung).
    custom_alpha = "".join(chr(c) for c in range(0x370, 0x370 + 128) if chr(c).isprintable())
    custom_alpha = custom_alpha[:128] if len(custom_alpha) >= 128 else (
        custom_alpha + "".join(chr(c) for c in range(0x400, 0x400 + (128 - len(custom_alpha))))
    )
    assert len(custom_alpha) == 128 and len(set(custom_alpha)) == 128
    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "150", "--alphabet", custom_alpha])
    assert r.returncode == 0, r.stderr
    print(" encode stderr:", r.stderr.strip().splitlines()[-1])

    r2 = run([sys.executable, DECODE, enc, "-o", out])
    assert r2.returncode == 0, r2.stderr

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: Custom-Alphabet Roundtrip exakt")


def test_preset_utf8128():
    print("== Test 5: eingebautes Preset 'utf8128' ==")
    src = f"{WORKDIR}/t5_src.bin"
    enc = f"{WORKDIR}/t5_enc.txt"
    out = f"{WORKDIR}/t5_out"
    make_test_file(src, 8_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "150", "--preset", "utf8128"])
    assert r.returncode == 0, r.stderr
    print(" encode stderr:", r.stderr.strip().splitlines()[-1])

    r2 = run([sys.executable, DECODE, enc, "-o", out])
    assert r2.returncode == 0, r2.stderr

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: 'utf8128'-Preset-Roundtrip exakt")


def test_joined_lines():
    print("== Test 6: zusammengefuegte Zeilen (simuliert Zwischenablage-Artefakt) ==")
    src = f"{WORKDIR}/t6_src.bin"
    enc = f"{WORKDIR}/t6_enc.txt"
    joined = f"{WORKDIR}/t6_joined.txt"
    out = f"{WORKDIR}/t6_out"
    make_test_file(src, 15_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20"])
    assert r.returncode == 0, r.stderr

    with open(enc) as f:
        lines = f.read().split("\n")
    non_empty = [i for i, l in enumerate(lines) if l.strip()]
    assert len(non_empty) > 30, "Testdatei erzeugt zu wenige Zeilen fuer diesen Test"

    # zwei Nutzdatenzeilen ohne Trennzeichen verschmelzen (simuliert verlorenen
    # Zeilenumbruch an einer Copy/Paste-Nahtstelle) ...
    p1 = non_empty[-5]
    lines[p1] = lines[p1] + lines[p1 + 1]
    del lines[p1 + 1]

    # ... und an einer zweiten, unabhaengigen Stelle gleich drei Zeilen (weiter
    # vorne verarbeitet, damit sich die Indizes nicht mit p1 ueberschneiden).
    p2 = non_empty[-15]
    lines[p2] = lines[p2] + lines[p2 + 1] + lines[p2 + 2]
    del lines[p2 + 2]
    del lines[p2 + 1]

    with open(joined, "w") as f:
        f.write("\n".join(lines))

    r2 = run([sys.executable, DECODE, joined, "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())
    assert "zusammengefuegte" in r2.stderr, "Kein Hinweis auf erkannte zusammengefuegte Zeilen im stderr"
    assert not os.path.exists(f"{out}.part1"), "Datei sollte trotz Verschmelzung vollstaendig rekonstruiert werden"

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual, "Nach Wiederaufteilung stimmt die Datei nicht ueberein!"
    print(" OK: trotz zusammengefuegter Zeilen exakt rekonstruiert")


def test_pagination_basic():
    print("== Test 7: Seitenteilung (--lines), normaler Roundtrip ==")
    src = f"{WORKDIR}/t7_src.bin"
    enc = f"{WORKDIR}/t7_enc.txt"
    out = f"{WORKDIR}/t7_out"
    make_test_file(src, 30_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20", "--lines", "20"])
    assert r.returncode == 0, r.stderr
    print(" encode stderr:", r.stderr.strip())

    r2 = run([sys.executable, DECODE, enc, "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: paginierter Roundtrip exakt")


def test_pagination_shuffled():
    print("== Test 8: Seiten gemischt in einer Datei ==")
    src = f"{WORKDIR}/t8_src.bin"
    enc = f"{WORKDIR}/t8_enc.txt"
    shuffled = f"{WORKDIR}/t8_shuffled.txt"
    out = f"{WORKDIR}/t8_out"
    make_test_file(src, 30_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20", "--lines", "15"])
    assert r.returncode == 0, r.stderr

    with open(enc) as f:
        lines = f.read().split("\n")
    pages = split_pages(lines)
    assert len(pages) >= 4, f"Testdatei erzeugt zu wenige Seiten ({len(pages)}) fuer diesen Test"
    random.shuffle(pages)

    with open(shuffled, "w") as f:
        for i, page in enumerate(pages):
            f.write("\n".join(page) + "\n")
            if i < len(pages) - 1:
                f.write("\n\n\n")

    r2 = run([sys.executable, DECODE, shuffled, "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: gemischte Seiten korrekt zusammengesetzt")


def test_pagination_separate_files():
    print("== Test 9: Seiten als einzelne Dateien ==")
    src = f"{WORKDIR}/t9_src.bin"
    enc = f"{WORKDIR}/t9_enc.txt"
    out = f"{WORKDIR}/t9_out"
    make_test_file(src, 25_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20", "--lines", "10"])
    assert r.returncode == 0, r.stderr

    with open(enc) as f:
        lines = f.read().split("\n")
    pages = split_pages(lines)
    assert len(pages) >= 3, f"Testdatei erzeugt zu wenige Seiten ({len(pages)}) fuer diesen Test"

    page_paths = []
    for i, page in enumerate(pages):
        p = f"{WORKDIR}/t9_page{i}.txt"
        with open(p, "w") as f:
            f.write("\n".join(page) + "\n")
        page_paths.append(p)

    r2 = run([sys.executable, DECODE, *page_paths, "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: als Einzeldateien korrekt zusammengesetzt")


def test_pagination_pattern():
    print("== Test 10: Seiten ueber Glob-Pattern ==")
    src = f"{WORKDIR}/t10_src.bin"
    enc = f"{WORKDIR}/t10_enc.txt"
    out = f"{WORKDIR}/t10_out"
    make_test_file(src, 25_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20", "--lines", "10"])
    assert r.returncode == 0, r.stderr

    with open(enc) as f:
        lines = f.read().split("\n")
    pages = split_pages(lines)
    assert len(pages) >= 3, f"Testdatei erzeugt zu wenige Seiten ({len(pages)}) fuer diesen Test"

    for i, page in enumerate(pages):
        with open(f"{WORKDIR}/t10_page{i:03d}.txt", "w") as f:
            f.write("\n".join(page) + "\n")

    r2 = run([sys.executable, DECODE, f"{WORKDIR}/t10_page*.txt", "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: ueber Glob-Pattern korrekt zusammengesetzt")


def test_pagination_missing_page():
    print("== Test 11: komplette Seite fehlt ==")
    src = f"{WORKDIR}/t11_src.bin"
    enc = f"{WORKDIR}/t11_enc.txt"
    damaged = f"{WORKDIR}/t11_damaged.txt"
    out = f"{WORKDIR}/t11_out"
    make_test_file(src, 25_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20", "--lines", "10"])
    assert r.returncode == 0, r.stderr

    with open(enc) as f:
        lines = f.read().split("\n")
    pages = split_pages(lines)
    assert len(pages) >= 4, f"Testdatei erzeugt zu wenige Seiten ({len(pages)}) fuer diesen Test"
    victim = len(pages) // 2  # Seiten sind hier noch unverschuffelt: Index == page_number
    del pages[victim]

    with open(damaged, "w") as f:
        for i, page in enumerate(pages):
            f.write("\n".join(page) + "\n")
            if i < len(pages) - 1:
                f.write("\n\n\n")

    r2 = run([sys.executable, DECODE, damaged, "-o", out])
    assert r2.returncode == EXIT_INCOMPLETE, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(f"{out}.errors.txt") as f:
        report = f.read()
    assert f"Seite {victim}: komplett fehlend" in report, report
    print(" OK: fehlende Seite im Fehlerbericht korrekt gemeldet")


def test_preset_utf16le128():
    print("== Test 12: eingebautes Preset 'utf16le128' (Datei muss echtes UTF-16LE sein) ==")
    src = f"{WORKDIR}/t12_src.bin"
    enc = f"{WORKDIR}/t12_enc.txt"
    out = f"{WORKDIR}/t12_out"
    make_test_file(src, 8_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "150", "--preset", "utf16le128"])
    assert r.returncode == 0, r.stderr
    print(" encode stderr:", r.stderr.strip().splitlines()[-1])

    with open(enc, "rb") as f:
        raw = f.read()
    assert raw[:2] == b"\xff\xfe", "Ausgabedatei hat kein fuehrendes UTF-16LE-BOM"
    assert raw[3] == 0, "Rohbytes sehen nicht nach UTF-16LE aus (High-Byte des ersten Zeichens != 0)"

    r2 = run([sys.executable, DECODE, enc, "-o", out])
    assert r2.returncode == 0, r2.stderr

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: 'utf16le128'-Preset-Roundtrip exakt, Datei ist echtes UTF-16LE mit BOM")


def test_preset_utf16le256():
    print("== Test 13: eingebautes Preset 'utf16le256' ==")
    src = f"{WORKDIR}/t13_src.bin"
    enc = f"{WORKDIR}/t13_enc.txt"
    out = f"{WORKDIR}/t13_out"
    make_test_file(src, 8_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "150", "--preset", "utf16le256"])
    assert r.returncode == 0, r.stderr
    print(" encode stderr:", r.stderr.strip().splitlines()[-1])

    with open(enc, "rb") as f:
        raw = f.read()
    assert raw[:2] == b"\xff\xfe", "Ausgabedatei hat kein fuehrendes UTF-16LE-BOM"
    assert raw[3] == 0, "Rohbytes sehen nicht nach UTF-16LE aus (High-Byte des ersten Zeichens != 0)"

    r2 = run([sys.executable, DECODE, enc, "-o", out])
    assert r2.returncode == 0, r2.stderr

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: 'utf16le256'-Preset-Roundtrip exakt")


def test_utf16le_pagination_separate_files():
    print("== Test 14: utf16le128 + Seitenteilung, Einzeldateien ohne BOM auf Folgeseiten ==")
    src = f"{WORKDIR}/t14_src.bin"
    enc = f"{WORKDIR}/t14_enc.txt"
    out = f"{WORKDIR}/t14_out"
    make_test_file(src, 25_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20",
             "--lines", "10", "--preset", "utf16le128"])
    assert r.returncode == 0, r.stderr

    with open(enc, "rb") as f:
        raw = f.read()
    assert raw[:2] == b"\xff\xfe"
    lines = raw[2:].decode("utf-16-le").split("\n")
    pages = split_pages(lines)
    assert len(pages) >= 3, f"Testdatei erzeugt zu wenige Seiten ({len(pages)}) fuer diesen Test"

    page_paths = []
    for i, page in enumerate(pages):
        p = f"{WORKDIR}/t14_page{i}.txt"
        # Bewusst OHNE BOM schreiben - simuliert eine aus der Gesamtdatei
        # herausgeschnittene Einzelseite, die den fuehrenden BOM nie gesehen
        # hat. decode.py muss UTF-16LE trotzdem am Byte-Muster erkennen.
        with open(p, "w", encoding="utf-16-le", newline="\n") as f:
            f.write("\n".join(page) + "\n")
        page_paths.append(p)

    r2 = run([sys.executable, DECODE, *page_paths, "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: UTF-16LE-Einzelseiten ohne BOM korrekt erkannt und zusammengesetzt")


def test_output_pattern_split():
    print("== Test 15: -o mit '*'-Platzhalter (encode.py schreibt eine Datei pro Seite) ==")
    src = f"{WORKDIR}/t15_src.bin"
    out = f"{WORKDIR}/t15_out"
    pattern = f"{WORKDIR}/t15_page*.enc"
    make_test_file(src, 25_000)

    r = run([sys.executable, ENCODE, src, "-o", pattern, "--width", "80", "--redundancy", "20", "--lines", "10"])
    assert r.returncode == 0, r.stderr
    print(" encode stderr:", r.stderr.strip())

    files = sorted(glob.glob(f"{WORKDIR}/t15_page*.enc"))
    # page_count laesst sich aus der encode-stderr-Ausgabe ablesen ("N Seiten")
    page_count = int(r.stderr.split("Seitenteilung:")[1].split(" Seiten")[0].strip())
    assert len(files) == page_count, f"{len(files)} Dateien statt {page_count} erzeugt"
    expected_width = len(str(page_count))
    for i, path in enumerate(files, start=1):
        suffix = os.path.basename(path)[len("t15_page"):-len(".enc")]
        assert suffix == str(i).zfill(expected_width), f"Nummerierung falsch: {suffix!r} (erwartet Breite {expected_width})"

    r2 = run([sys.executable, DECODE, pattern, "-o", out])
    assert r2.returncode == 0, r2.stderr
    print(" decode stderr:", r2.stderr.strip())

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(f" OK: {page_count} Dateien mit optimaler Nullauffuellung erzeugt und korrekt zusammengesetzt")


def test_output_pattern_split_utf16le_boms():
    print("== Test 16: -o mit '*' + utf16le128 - jede Datei hat ihr eigenes BOM ==")
    src = f"{WORKDIR}/t16_src.bin"
    out = f"{WORKDIR}/t16_out"
    pattern = f"{WORKDIR}/t16_page*.enc"
    make_test_file(src, 20_000)

    r = run([sys.executable, ENCODE, src, "-o", pattern, "--width", "80", "--redundancy", "20",
             "--lines", "15", "--preset", "utf16le128"])
    assert r.returncode == 0, r.stderr

    files = sorted(glob.glob(f"{WORKDIR}/t16_page*.enc"))
    assert len(files) >= 2, f"Testdatei erzeugt zu wenige Seiten ({len(files)}) fuer diesen Test"
    for path in files:
        with open(path, "rb") as f:
            bom = f.read(2)
        assert bom == b"\xff\xfe", f"{path} hat kein eigenes BOM ({bom!r})"

    r2 = run([sys.executable, DECODE, pattern, "-o", out])
    assert r2.returncode == 0, r2.stderr

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(f" OK: alle {len(files)} Dateien haben ein eigenes BOM, Roundtrip exakt")


def test_output_pattern_single_bom_without_star():
    print("== Test 17: --lines ohne '*' in -o -> weiterhin nur EIN BOM im Gesamtstrom ==")
    src = f"{WORKDIR}/t17_src.bin"
    enc = f"{WORKDIR}/t17_enc.txt"
    out = f"{WORKDIR}/t17_out"
    make_test_file(src, 20_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20",
             "--lines", "15", "--preset", "utf16le128"])
    assert r.returncode == 0, r.stderr

    with open(enc, "rb") as f:
        raw = f.read()
    assert raw[:2] == b"\xff\xfe", "Gesamtstrom hat kein fuehrendes BOM"
    bom_occurrences = raw[2:].decode("utf-16-le").count(chr(0xFEFF))
    assert bom_occurrences == 0, f"{bom_occurrences} zusaetzliche(s) BOM(s) mitten im Gesamtstrom gefunden"

    r2 = run([sys.executable, DECODE, enc, "-o", out])
    assert r2.returncode == 0, r2.stderr

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: genau ein BOM im Gesamtstrom, Roundtrip exakt")


def test_latin128_saved_as_latin1():
    print("== Test 18: latin128-Seiten als Latin-1 ('ANSI') gespeichert ==")
    src = f"{WORKDIR}/t18_src.bin"
    enc = f"{WORKDIR}/t18_enc*.txt"
    out = f"{WORKDIR}/t18_out"
    for old in glob.glob(f"{WORKDIR}/t18_enc*.txt"):
        os.remove(old)
    make_test_file(src, 8_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--preset", "latin128", "--lines", "38"])
    assert r.returncode == 0, r.stderr
    pages = sorted(glob.glob(f"{WORKDIR}/t18_enc*.txt"))
    assert len(pages) > 1
    # Simuliert OCR-Tool/Editor, das die Seiten als Latin-1 statt UTF-8 speichert
    for path in pages:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        with open(path, "w", encoding="latin-1", newline="") as f:
            f.write(text)

    r2 = run([sys.executable, DECODE, *pages, "-o", out])
    assert r2.returncode == 0, r2.stderr
    assert "Latin-1" in r2.stderr

    with open(src, "rb") as f:
        expected = f.read()
    with open(out, "rb") as f:
        actual = f.read()
    assert expected == actual
    print(" OK: Latin-1-Seiten exakt rekonstruiert")


def test_header_only():
    print("== Test 19: --header gibt nur die Header aller Dateien aus ==")
    src = f"{WORKDIR}/t19_src.bin"
    enc = f"{WORKDIR}/t19_enc*.txt"
    out = f"{WORKDIR}/t19_out"
    for old in glob.glob(f"{WORKDIR}/t19_enc*.txt") + glob.glob(f"{out}*"):
        os.remove(old)
    make_test_file(src, 5_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--lines", "20"])
    assert r.returncode == 0, r.stderr
    pages = sorted(glob.glob(f"{WORKDIR}/t19_enc*.txt"))

    r2 = run([sys.executable, DECODE, "--header", *pages, "-o", out])
    assert r2.returncode == 0, r2.stderr
    for path in pages:
        assert f"== {path} ==" in r2.stdout
    for n in range(len(pages)):
        assert f"Seite:{' ' * 15}{n} (0-basiert) von {len(pages)}" in r2.stdout, r2.stdout
    assert "Dateigroesse:        5000 Bytes" in r2.stdout
    assert not glob.glob(f"{out}*"), "--header darf keine Ausgabedatei schreiben"
    print(f" OK: {len(pages)} Header ausgegeben, keine Ausgabedatei geschrieben")


def test_columns():
    print("== Test 20: --columns verteilt jede Seite auf mehrere Spalten ==")
    src = f"{WORKDIR}/t20_src.bin"
    enc = f"{WORKDIR}/t20_enc.txt"
    out = f"{WORKDIR}/t20_out"
    make_test_file(src, 7_000)
    with open(src, "rb") as f:
        expected = f.read()

    for columns, extra in [(2, []), (3, ["--lines", "17"]), (4, ["--preset", "utf16le128", "--lines", "10"])]:
        r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "60", "--columns", str(columns), *extra])
        assert r.returncode == 0, r.stderr
        with open(enc, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-16") if "utf16le128" in extra else raw.decode("utf-8")
        rows = [l for l in text.split("\n") if l.strip()]
        assert max(l.count(" ### ") for l in rows) == columns - 1, f"{columns} Spalten erwartet"
        assert all(len(l) <= columns * 60 + (columns - 1) * 5 for l in rows)

        # Praeambel und Header bleiben einspaltig; --lines zaehlt die Zeilen
        # nach dem Aufteilen in Spalten (Header nicht mitgezaehlt).
        for page in text.lstrip("﻿").split("\n\n\n\n"):
            page_rows = [l for l in page.split("\n") if l.strip()]
            first_multi = next(i for i, l in enumerate(page_rows) if " ### " in l)
            assert all(" ### " not in l for l in page_rows[:first_multi])
            assert first_multi > 3, "Praeambel und Header erwartet"
            if "--lines" in extra:
                lines = int(extra[extra.index("--lines") + 1])
                assert len(page_rows) - first_multi <= lines, f"mehr als {lines} Nutzdatenzeilen auf einer Seite"

        r = run([sys.executable, DECODE, enc, "-o", out])
        assert r.returncode == 0, r.stderr
        with open(out, "rb") as f:
            assert f.read() == expected, f"Roundtrip mit --columns {columns} fehlgeschlagen"
    print(" OK: Roundtrip mit 2/3/4 Spalten (inkl. Seitenteilung und UTF-16LE)")


def test_show_preset():
    print("== Test 21: --show-preset gibt alle verwendeten Zeichen eines Presets aus ==")
    src = f"{WORKDIR}/t21_src.bin"
    enc = f"{WORKDIR}/t21_enc.txt"
    make_test_file(src, 3_000)

    for preset, codec in [("default", "utf-8"), ("latin128", "utf-8"), ("utf16le256", "utf-16"), ("utf32le512", "utf-32")]:
        r = subprocess.run([sys.executable, ENCODE, "--show-preset", preset], capture_output=True)
        assert r.returncode == 0, r.stderr
        shown = r.stdout.decode("utf-8")
        assert shown.endswith("\n") and shown.count("\n") == 1, "genau eine Zeile erwartet"
        charset = shown[:-1]
        assert len(set(charset)) == len(charset), "Zeichen doppelt ausgegeben"
        assert set(DEFAULT_ALPHABET) <= set(charset), "Header-Alphabet fehlt"

        r = run([sys.executable, ENCODE, src, "-o", enc, "--preset", preset, "--columns", "2", "--lines", "20"])
        assert r.returncode == 0, r.stderr
        with open(enc, "rb") as f:
            text = f.read().decode(codec).lstrip("﻿")
        unexpected = set(text) - set(charset) - {" ", "\n"}
        assert not unexpected, f"{preset}: Zeichen fehlen in --show-preset: {sorted(unexpected)!r}"
    print(" OK: --show-preset deckt alle Zeichen der kodierten Ausgabe ab")


def payload_cells(text):
    """Nutzdaten-Zeilen (ohne Praeambel/Header) eines einspaltigen encode.py-Outputs."""
    lines = [l for l in text.split("\n") if l.strip()]
    header_alpha = Alphabet(DEFAULT_ALPHABET)
    start = find_preamble_positions(lines, header_alpha)[0]
    n_header = HEADER_COPIES * parse_preamble(lines[start].strip(), header_alpha)
    return lines[start + 1 + n_header:]


def longest_run(s):
    best = cur = 1
    for a, b in zip(s, s[1:]):
        cur = cur + 1 if a == b else 1
        best = max(best, cur)
    return best


def test_scrambling():
    print("== Test 22: Verwuerfelung bricht lange Folgen gleicher Zeichen auf ==")
    src = f"{WORKDIR}/t22_src.bin"
    enc = f"{WORKDIR}/t22_enc.txt"
    out = f"{WORKDIR}/t22_out"
    # stark repetitive Daten (wie bzip2-/Base64-Bloecke mit vielen gleichen Bytes)
    expected = bytes(4_000) + b"A" * 3_000 + bytes(random.getrandbits(8) for _ in range(2_000))
    with open(src, "wb") as f:
        f.write(expected)

    max_runs = {}
    for flag in ([], ["--no-scramble"]):
        r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", *flag])
        assert r.returncode == 0, r.stderr
        with open(enc, encoding="utf-8") as f:
            cells = payload_cells(f.read())
        max_runs[bool(flag)] = max(longest_run(c[10:]) for c in cells)

        r = run([sys.executable, DECODE, enc, "--header"])
        assert r.returncode == 0, r.stderr
        assert "Verwuerfelt:" in r.stdout and ("nein" if flag else "ja") in r.stdout.split("Verwuerfelt:")[1].split("\n")[0]

        r = run([sys.executable, DECODE, enc, "-o", out])
        assert r.returncode == 0, r.stderr
        with open(out, "rb") as f:
            assert f.read() == expected, f"Roundtrip {flag or 'verwuerfelt'} fehlgeschlagen"

    print(f" laengste Folge: verwuerfelt {max_runs[False]}, unverwuerfelt {max_runs[True]}")
    assert max_runs[True] >= 30, "Testdaten sollten ohne Verwuerfelung lange Folgen erzeugen"
    assert max_runs[False] <= 6, "Verwuerfelung hat lange Folgen nicht aufgebrochen"
    print(" OK: verwuerfelt ohne lange Folgen, beide Varianten exakt rekonstruiert")


def _run_starts(s, start, min_len):
    """Startindizes der Folgen gleicher Zeichen mit Laenge >= min_len ab `start`."""
    out = []
    i = start
    while i < len(s):
        j = i
        while j < len(s) and s[j] == s[i]:
            j += 1
        if j - i >= min_len:
            out.append(i)
        i = j
    return out


def test_shift_repair():
    print("== Test 23: Laengenreparatur bei falsch gezaehlten Folgen gleicher Zeichen ==")
    src = f"{WORKDIR}/t23_src.bin"
    enc = f"{WORKDIR}/t23_enc.txt"
    shifted = f"{WORKDIR}/t23_shifted.txt"
    out = f"{WORKDIR}/t23_out"
    make_test_file(src, 15_000)

    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80", "--redundancy", "20"])
    assert r.returncode == 0, r.stderr
    with open(enc, encoding="utf-8") as f:
        text = f.read()
    cells = payload_cells(text)
    prefix = 10  # Zeilennummer+CRC nie anfassen (wie in den anderen Tests)

    # Zeilen mit zwei weit auseinanderliegenden Folgen >= 2 im Nutzdatenteil:
    # Die Verschiebung muss deutlich mehr als floor(r/2) Zeichen betreffen,
    # sonst korrigiert RS sie schon ohne Reparatur (dann gaebe es nichts zu testen).
    def first_last(c):
        runs = _run_starts(c, prefix, 2)
        return runs[0], runs[-1]

    def far_apart(c):
        runs = _run_starts(c, prefix, 2)
        return len(runs) >= 2 and runs[0] <= 30 and runs[-1] - runs[0] >= 20
    candidates = [c for c in cells if far_apart(c)]
    assert len(candidates) >= 5, "zu wenige Zeilen mit Folgen gleicher Zeichen"
    picked = random.sample(candidates, 5)

    def drop(s, pos):
        return s[:pos] + s[pos + 1:]

    def dup(s, pos):
        return s[:pos] + s[pos] + s[pos:]

    edits = {}
    a, b = first_last(picked[0])
    edits[picked[0]] = dup(picked[0], a)                    # eine Folge zu lang
    a, b = first_last(picked[1])
    edits[picked[1]] = drop(picked[1], a)                   # eine Folge zu kurz
    a, b = first_last(picked[2])
    edits[picked[2]] = drop(dup(picked[2], b), a)           # eine zu lang, eine zu kurz (gleiche Laenge)
    a, b = first_last(picked[3])
    edits[picked[3]] = dup(dup(picked[3], b), a)            # zwei Folgen zu lang
    a, b = first_last(picked[4])
    edits[picked[4]] = corrupt_symbols_in_line(drop(picked[4], a), 1, protect_prefix=prefix,
                                               alphabet_chars=DEFAULT_ALPHABET)  # zu kurz + Symbolfehler

    for orig, changed in edits.items():
        assert text.count(orig) == 1
        text = text.replace(orig, changed)
    with open(shifted, "w", encoding="utf-8") as f:
        f.write(text)

    for stale in (f"{out}.part1", f"{out}.part2"):  # Reste frueherer Laeufe
        if os.path.exists(stale):
            os.remove(stale)
    r = run([sys.executable, DECODE, shifted, "-o", out])
    assert r.returncode == 0, r.stderr
    print(" decode stderr:", r.stderr.strip())
    assert "Laengenreparatur" in r.stderr, "Kein Hinweis auf Laengenreparatur im stderr"
    assert not os.path.exists(f"{out}.part1"), "Datei sollte trotz Verschiebungen vollstaendig rekonstruiert werden"
    with open(f"{out}.errors.txt") as f:
        report = f.read()
    assert "Per Laengenreparatur gerettete Zeilen (5)" in report, report
    with open(src, "rb") as f:
        assert f.read() == open(out, "rb").read(), "Nach Laengenreparatur stimmt die Datei nicht ueberein!"
    print(" OK: 5 verschobene Zeilen repariert, Datei exakt rekonstruiert")


def payload_line_indices(lines):
    """Indizes der Nutzdatenzeilen einer einseitigen encode.py-Ausgabe."""
    header_alpha = Alphabet(DEFAULT_ALPHABET)
    (start,) = find_preamble_positions(lines, header_alpha)
    first = start + 1 + parse_preamble(lines[start], header_alpha) * HEADER_COPIES
    return [i for i in range(first, len(lines)) if lines[i].strip()]


def test_empty_file():
    print("== Test 24: leere Eingabedatei ==")
    src, enc, out = f"{WORKDIR}/t24_src.bin", f"{WORKDIR}/t24_enc.txt", f"{WORKDIR}/t24_out"
    open(src, "wb").close()
    r = run([sys.executable, ENCODE, src, "-o", enc])
    assert r.returncode == 0, r.stderr
    r = run([sys.executable, DECODE, enc, "-o", out])
    assert r.returncode == 0, r.stderr
    assert os.path.getsize(out) == 0, "leere Datei muss leer rekonstruiert werden"
    print(" OK: leere Datei exakt rekonstruiert")


def test_line_number_errors():
    print("== Test 25: Lesefehler in Zeilennummern ==")
    src, enc, out = f"{WORKDIR}/t25_src.bin", f"{WORKDIR}/t25_enc.txt", f"{WORKDIR}/t25_out"
    make_test_file(src, 20_000)
    r = run([sys.executable, ENCODE, src, "-o", enc, "--width", "80"])
    assert r.returncode == 0, r.stderr
    with open(enc) as f:
        lines = f.read().split("\n")
    idx = payload_line_indices(lines)
    assert len(idx) > 100
    # (a) Zeilennummer weit ausserhalb des gueltigen Bereichs, (b) Zeilennummer
    # einer anderen, vorhandenen Zeile (Kollision). Je nur EIN Zeichen geaendert.
    lines[idx[5]] = "!" + lines[idx[5]][1:]
    lines[idx[20]] = "3" + lines[idx[20]][1:]
    with open(enc, "w") as f:
        f.write("\n".join(lines))
    r = run([sys.executable, DECODE, enc, "-o", out])
    assert r.returncode == EXIT_INCOMPLETE, r.stderr
    with open(f"{out}.errors.txt") as f:
        report = f.read()
    assert "Fehlerhafte/fehlende Zeilen (2):" in report, report
    assert "Zeile 5: fehlend" in report and "Zeile 20: fehlend" in report, report
    print(" OK: nur die beiden betroffenen Zeilen fehlen (keine Folgefehler)")


def test_hash_mismatch_exit_code():
    print("== Test 26: Exitcode bei fehlgeschlagener SHA-256-Pruefung ==")
    src, enc, out = f"{WORKDIR}/t26_src.bin", f"{WORKDIR}/t26_enc.txt", f"{WORKDIR}/t26_out"
    make_test_file(src, 5_000)
    r = run([sys.executable, ENCODE, src, "-o", enc, "--no-scramble"])
    assert r.returncode == 0, r.stderr
    with open(enc) as f:
        lines = f.read().split("\n")
    a, b = payload_line_indices(lines)[3:5]
    # Zeilennummern zweier Nachbarzeilen vertauschen (Zeilennummernbreite 2):
    # jede Zeile bleibt fuer sich gueltig, die Datei als Ganzes ist falsch.
    lines[a], lines[b] = lines[b][:2] + lines[a][2:], lines[a][:2] + lines[b][2:]
    with open(enc, "w") as f:
        f.write("\n".join(lines))
    r = run([sys.executable, DECODE, enc, "-o", out])
    assert r.returncode == EXIT_HASH_MISMATCH, (r.returncode, r.stderr)
    with open(f"{out}.errors.txt") as f:
        assert "SHA-256-Pruefung: FEHLGESCHLAGEN" in f.read()
    print(" OK: Exitcode", r.returncode)


def test_too_many_pages():
    print("== Test 27: mehr Seiten als das Header-Feld fasst ==")
    src = f"{WORKDIR}/t27_src.bin"
    make_test_file(src, 100_000)
    r = run([sys.executable, ENCODE, src, "-o", f"{WORKDIR}/t27_enc.txt", "--width", "12", "--lines", "1"])
    assert r.returncode == 2 and "Seiten" in r.stderr and "Traceback" not in r.stderr, r.stderr
    print(" OK: verstaendliche Fehlermeldung statt Traceback")


def test_reframe():
    print("== Test 28: reframe.py ==")
    enc, out = f"{WORKDIR}/t28_enc.txt", f"{WORKDIR}/t28_out"
    b64 = base64.encodebytes(os.urandom(3_001))  # mit Zeilenumbruechen und '='-Padding
    r = subprocess.run([sys.executable, REFRAME, "-o", enc], input=b64, capture_output=True)
    assert r.returncode == 0, r.stderr
    r = run([sys.executable, DECODE, enc, "-o", out])
    assert r.returncode == 0, r.stderr
    with open(out, "rb") as f:
        assert f.read() == b64.replace(b"\n", b"").rstrip(b"=")

    for bad, msg in ((b"QUJD=REVG\n", "nicht am Ende"), (b"QUJD\xc3\xa4\n", "kein gueltiges")):
        r = subprocess.run([sys.executable, REFRAME], input=bad, capture_output=True, text=False)
        stderr = r.stderr.decode()
        assert r.returncode == 1 and msg in stderr and "Traceback" not in stderr, stderr
    print(" OK: Roundtrip exakt, ungueltige Eingaben sauber abgelehnt")


def test_wide_crc():
    print("== Test 29: CRC erfasst bei GF512 das oberste Symbolbit ==")
    assert symbols_crc32([0x100], wide=True) != symbols_crc32([0x000], wide=True)
    assert symbols_crc32([0x100], wide=False) == symbols_crc32([0x000], wide=False)  # alte Variante
    print(" OK")


@unittest.skipUnless(shutil.which("git"), "git nicht installiert")
def test_archive2git():
    print("== Test 30: archive2git.py ==")
    base = tempfile.mkdtemp(dir=WORKDIR)
    src, repo, tbz = f"{base}/src", f"{base}/repo", f"{base}/a.tar.bz2"
    os.makedirs(f"{src}/proj/sub")
    with open(f"{src}/proj/a.txt", "w") as f:
        f.write("hallo\n")
    os.link(f"{src}/proj/a.txt", f"{src}/proj/hard.txt")
    with tarfile.open(tbz, "w:bz2") as tar:
        tar.add(f"{src}/proj", arcname="proj")

    def git(*args):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout

    os.makedirs(repo)
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    with open(f"{repo}/staged.txt", "w") as f:
        f.write("gestaged\n")
    git("add", "staged.txt")
    git("commit", "-q", "-m", "vorher")

    with open(tbz, "rb") as f:
        r = subprocess.run([sys.executable, ARCHIVE2GIT, "--strip", "proj"], input=f.read(),
                           capture_output=True, cwd=repo)
    assert r.returncode == 0, r.stderr
    assert git("ls-tree", "-r", "--name-only", "HEAD").split() == ["a.txt", "hard.txt"]
    assert git("show", "HEAD:hard.txt") == "hallo\n", "Hardlink nicht aufgeloest"
    assert git("rev-list", "--count", "HEAD").strip() == "2", "bisherige History verloren"

    r = subprocess.run([sys.executable, ARCHIVE2GIT], input=b"kein archiv", capture_output=True, cwd=repo)
    assert r.returncode == 1 and b"Traceback" not in r.stderr, r.stderr
    print(" OK: Import als neuer Commit, Hardlink aufgeloest, Fehler sauber gemeldet")


def load_tests(loader, tests, pattern):
    """unittest-Einstieg: jede test_*-Funktion als eigener Testfall, in
    Definitionsreihenfolge (pytest sammelt die Funktionen direkt)."""
    suite = unittest.TestSuite()
    for name, obj in list(globals().items()):
        if name.startswith("test_") and callable(obj):
            suite.addTest(unittest.FunctionTestCase(obj))
    return suite


if __name__ == "__main__":
    unittest.main()
