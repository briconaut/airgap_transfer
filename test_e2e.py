import os
import random
import subprocess
import sys
import hashlib

# lib/ fuer den direkten Import (Alphabet wird in den Hilfsfunktionen benutzt)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))
from alphabets import DEFAULT_ALPHABET

# Pfade zu den Tools relativ zu diesem Skript
ENCODE  = os.path.join(os.path.dirname(__file__), "bin", "encode.py")
DECODE  = os.path.join(os.path.dirname(__file__), "bin", "decode.py")
REFRAME = os.path.join(os.path.dirname(__file__), "bin", "reframe.py")

random.seed(7)
WORKDIR = "/home/claude/airgap_transfer/e2e"
os.makedirs(WORKDIR, exist_ok=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r


def make_test_file(path, size):
    with open(path, "wb") as f:
        f.write(bytes(random.getrandbits(8) for _ in range(size)))


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
    assert r2.returncode == 0, r2.stderr
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


if __name__ == "__main__":
    test_clean_roundtrip()
    test_correctable_corruption()
    test_lost_lines()
    test_custom_alphabet()
    test_preset_utf8128()
    test_joined_lines()
    print("\nAlle End-to-End-Tests bestanden.")
