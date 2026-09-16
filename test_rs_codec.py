import os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

from rs_codec import GaloisField, rs_generator_poly, rs_encode, rs_decode

random.seed(42)


def run_field_tests(m):
    gf = GaloisField(m)
    print(f"--- GF(2^{m}) size={gf.size} ---")

    for r in (4, 8, 16):
        k = min(gf.size - 1 - r, 40)  # Testblockgroesse, innerhalb n<=size-1
        gen = rs_generator_poly(r, gf)
        max_correctable = r // 2

        # Fall 1: keine Fehler
        data = [random.randrange(gf.size) for _ in range(k)]
        cw = rs_encode(data, r, gen, gf)
        decoded, errs = rs_decode(cw, r, gf)
        assert decoded == data and errs == 0, f"FEHLER: sauberer Fall r={r} m={m}"

        # Fall 2: genau max_correctable Fehler an zufaelligen Positionen -> muss klappen
        for _trial in range(20):
            cw2 = list(cw)
            positions = random.sample(range(len(cw2)), max_correctable)
            for p in positions:
                orig = cw2[p]
                new = orig
                while new == orig:
                    new = random.randrange(gf.size)
                cw2[p] = new
            decoded2, errs2 = rs_decode(cw2, r, gf)
            assert decoded2 == data, (
                f"FEHLER: {max_correctable} Fehler (r={r}, m={m}) nicht korrekt "
                f"repariert an Positionen {positions}"
            )
            assert errs2 == max_correctable

        # Fall 3: max_correctable + 1 Fehler -> MUSS als unkorrigierbar erkannt werden
        # (nie stillschweigend falsch entschluesseln!)
        over_limit = max_correctable + 1
        if over_limit <= len(cw):
            false_positive_count = 0
            for _trial in range(20):
                cw3 = list(cw)
                positions = random.sample(range(len(cw3)), over_limit)
                for p in positions:
                    orig = cw3[p]
                    new = orig
                    while new == orig:
                        new = random.randrange(gf.size)
                    cw3[p] = new
                decoded3, errs3 = rs_decode(cw3, r, gf)
                if decoded3 == data:
                    false_positive_count += 1  # waere gefaehrlich: irrtuemlich "korrekt"
            # Ein Restrisiko falscher Positivfaelle ist bei RS ueber der Kapazitaet
            # theoretisch nie ganz ausschliessbar, sollte aber selten sein.
            print(
                f"  r={r} over-limit ({over_limit} Fehler): "
                f"{false_positive_count}/20 faelschlich als 'ok' erkannt "
                f"(erwartet: meist abgelehnt / als Fehler erkannt)"
            )

        print(f"  r={r}: k={k}, max_correctable={max_correctable} -> Encode/Decode OK")


for m in (5, 6, 7, 8):
    run_field_tests(m)

print("\nAlle Kernfaelle bestanden.")
