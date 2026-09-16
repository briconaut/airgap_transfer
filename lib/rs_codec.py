"""
Reed-Solomon Codec ueber GF(2^m), m in {5,6,7,8} (Alphabetgroessen 32/64/128/256).
Reine Python-Stdlib, keine Zusatzpakete.

Konvention:
- Polynome sind Listen von Koeffizienten, HOECHSTER Grad zuerst (wie beim
  klassischen "long division"-Bild).
- Codewort = [Datensymbole...][Paritaetssymbole...] (systematischer Code).
- Generatorpolynom hat Nullstellen bei alpha^0 .. alpha^(r-1) (fcr=0).
- Fehlerkorrektur ohne bekannte Erasure-Positionen -> maximal floor(r/2)
  Symbolfehler pro Codewort korrigierbar. Das ist ein zentraler Fakt fuer
  die Wahl von --redundancy: r Paritaetssymbole korrigieren NICHT r Fehler,
  sondern nur floor(r/2).
"""

# Bekannte primitive Polynome je Feldgroesse (Standardwerte aus der Literatur,
# z.B. GF(256): 0x11D ist das in QR-Codes/AES verwendete Polynom).
PRIMITIVE_POLYS = {
    5: 0x25,   # x^5 + x^2 + 1
    6: 0x43,   # x^6 + x + 1
    7: 0x89,   # x^7 + x^3 + 1
    8: 0x11D,  # x^8 + x^4 + x^3 + x^2 + 1
}


class GaloisField:
    """GF(2^m) mit vorab berechneten exp/log-Tabellen fuer schnelle Multiplikation."""

    def __init__(self, m: int):
        if m not in PRIMITIVE_POLYS:
            raise ValueError(f"Keine primitive Polynom-Konstante fuer GF(2^{m}) hinterlegt")
        self.m = m
        self.size = 1 << m  # z.B. 64 fuer m=6
        prim = PRIMITIVE_POLYS[m]

        exp = [0] * (2 * self.size)
        log = [0] * self.size
        x = 1
        for i in range(self.size - 1):
            exp[i] = x
            log[x] = i
            x <<= 1
            if x & self.size:
                x ^= prim
        # Tabelle verdoppeln, damit gf_mul ohne Modulo auskommt
        for i in range(self.size - 1, 2 * (self.size - 1)):
            exp[i] = exp[i - (self.size - 1)]

        # Validierung: primitives Polynom muss durch ALLE 2^m-1 Werte zyklen.
        # Andernfalls wuerde die RS-Mathematik still falsche Ergebnisse liefern
        # statt laut zu scheitern -> das darf nicht passieren.
        seen = set(exp[: self.size - 1])
        if len(seen) != self.size - 1 or 0 in seen:
            raise ValueError(
                f"Primitives Polynom 0x{prim:x} ist fuer GF(2^{m}) ungueltig "
                f"(Tabelle deckt nicht alle {self.size - 1} Werte ab)"
            )
        self.exp = exp
        self.log = log

    def mul(self, a: int, b: int) -> int:
        if a == 0 or b == 0:
            return 0
        return self.exp[self.log[a] + self.log[b]]

    def pow(self, a: int, power: int) -> int:
        if a == 0:
            return 0 if power != 0 else 1
        return self.exp[(self.log[a] * power) % (self.size - 1)]

    def inverse(self, a: int) -> int:
        if a == 0:
            raise ZeroDivisionError("0 hat kein Inverses in GF")
        return self.exp[(self.size - 1) - self.log[a]]


# ---------------------------------------------------------------------------
# Polynom-Hilfsfunktionen (Koeffizienten hoechster Grad zuerst)
# ---------------------------------------------------------------------------

def poly_scale(poly, scalar, gf: GaloisField):
    return [gf.mul(c, scalar) for c in poly]


def poly_add(p1, p2):
    # Addition in GF(2^m) == XOR, Polynome auf gleiche Laenge bringen
    if len(p1) < len(p2):
        p1, p2 = p2, p1
    diff = len(p1) - len(p2)
    out = list(p1)
    for i, c in enumerate(p2):
        out[i + diff] ^= c
    return out


def poly_mul(p1, p2, gf: GaloisField):
    out = [0] * (len(p1) + len(p2) - 1)
    for i, c1 in enumerate(p1):
        if c1 == 0:
            continue
        for j, c2 in enumerate(p2):
            out[i + j] ^= gf.mul(c1, c2)
    return out


def poly_eval(poly, x, gf: GaloisField):
    # Horner-Schema
    y = poly[0]
    for c in poly[1:]:
        y = gf.mul(y, x) ^ c
    return y


# ---------------------------------------------------------------------------
# Reed-Solomon
# ---------------------------------------------------------------------------

def rs_generator_poly(r: int, gf: GaloisField):
    """g(x) = Produkt_{i=0}^{r-1} (x - alpha^i), monic, hoechster Grad zuerst."""
    g = [1]
    for i in range(r):
        g = poly_mul(g, [1, gf.exp[i]], gf)
    return g


def rs_encode(data_symbols, r: int, gen, gf: GaloisField):
    """Systematische Kodierung: Rueckgabe = data_symbols + r Paritaetssymbole."""
    padded = list(data_symbols) + [0] * r
    for i in range(len(data_symbols)):
        coef = padded[i]
        if coef != 0:
            for j in range(1, len(gen)):
                padded[i + j] ^= gf.mul(gen[j], coef)
    parity = padded[len(data_symbols):]
    return list(data_symbols) + parity


def rs_calc_syndromes(codeword, r: int, gf: GaloisField):
    """S_i = codeword(alpha^i) fuer i=0..r-1. Alle 0 => kein Fehler."""
    return [poly_eval(codeword, gf.exp[i], gf) for i in range(r)]


def rs_find_error_locator(synd, r: int, gf: GaloisField):
    """Berlekamp-Massey. Liefert das Fehlerlokator-Polynom oder None bei
    zu vielen Fehlern (mehr als der Code korrigieren kann)."""
    err_loc = [1]
    old_loc = [1]
    for i in range(r):
        old_loc = old_loc + [0]
        delta = synd[i]
        for j in range(1, len(err_loc)):
            delta ^= gf.mul(err_loc[-(j + 1)], synd[i - j])
        if delta != 0:
            if len(old_loc) > len(err_loc):
                new_loc = poly_scale(old_loc, delta, gf)
                old_loc = poly_scale(err_loc, gf.inverse(delta), gf)
                err_loc = new_loc
            err_loc = poly_add(err_loc, poly_scale(old_loc, delta, gf))
    # fuehrende Nullen abschneiden
    while len(err_loc) > 1 and err_loc[0] == 0:
        err_loc.pop(0)
    errs = len(err_loc) - 1
    if errs * 2 > r:
        return None  # nachweislich zu viele Fehler fuer diesen Code
    return err_loc


def rs_find_errors(err_loc, n: int, gf: GaloisField):
    """Chien-Suche: Nullstellen von err_loc unter alpha^-i, i=0..n-1."""
    errs = len(err_loc) - 1
    err_pos = []
    for i in range(n):
        inv_root = gf.pow(gf.exp[i], gf.size - 2)  # alpha^-i = alpha^((size-1)-i), size-2 stellvertretend über pow
        # sauberer: alpha^-i direkt ueber Tabelle
        inv_root = gf.exp[(gf.size - 1 - i) % (gf.size - 1)] if i != 0 else 1
        if poly_eval(err_loc, inv_root, gf) == 0:
            err_pos.append(n - 1 - i)
    if len(err_pos) != errs:
        return None
    return err_pos


def _poly_eval_natural(poly, x, gf: GaloisField):
    """Wie poly_eval, aber fuer Koeffizientenlisten in NATUERLICHER Ordnung
    (poly[i] = Koeffizient von x^i, niedrigster Grad zuerst)."""
    y = 0
    xp = 1  # x^0
    for c in poly:
        if c:
            y ^= gf.mul(c, xp)
        xp = gf.mul(xp, x)
    return y


def rs_correct_errata(codeword, synd, err_pos, gf: GaloisField):
    """Forney-Algorithmus: berechnet Fehlerwerte und korrigiert das Codewort.

    Arbeitet intern in NATUERLICHER Koeffizientenordnung (Index = Exponent),
    da sich Lambda(x), Omega(x) und die Ableitung darin am saubersten fassen
    lassen. codeword/err_pos kommen im Array-Format des Moduls (Index 0 =
    hoechster Grad), daher die Umrechnung 'natuerlicher Exponent = n-1-pos'.
    """
    n = len(codeword)
    r = len(synd)

    natural_exponents = [n - 1 - p for p in err_pos]
    Xl = [gf.exp[e % (gf.size - 1)] for e in natural_exponents]

    # Lambda(x) = Produkt_l (1 + X_l * x), natuerliche Ordnung, Index = Exponent
    lam = [1]
    for x in Xl:
        new_lam = [0] * (len(lam) + 1)
        for i, c in enumerate(lam):
            new_lam[i] ^= c
            new_lam[i + 1] ^= gf.mul(c, x)
        lam = new_lam

    # Omega(x) = S(x) * Lambda(x) mod x^r  (synd ist bereits natuerliche Ordnung: S_0..S_{r-1})
    prod = [0] * (len(synd) + len(lam) - 1)
    for i, cs in enumerate(synd):
        if cs == 0:
            continue
        for j, cl in enumerate(lam):
            prod[i + j] ^= gf.mul(cs, cl)
    omega = prod[:r]

    # Lambda'(x): formale Ableitung. In Char 2 tragen nur ungerade Potenzen bei:
    # Koeffizient von x^k in Lambda' ist Lambda[k+1], falls (k+1) ungerade ist.
    lam_deriv = []
    for k in range(len(lam) - 1):
        lam_deriv.append(lam[k + 1] if (k + 1) % 2 == 1 else 0)

    corrected = list(codeword)
    for pos, x_l in zip(err_pos, Xl):
        xl_inv = gf.inverse(x_l)
        num = _poly_eval_natural(omega, xl_inv, gf)
        den = _poly_eval_natural(lam_deriv, xl_inv, gf)
        if den == 0:
            return None  # numerisch entartet -> defensiv als nicht korrigierbar behandeln
        # Zusaetzlicher Faktor X_l: unsere Syndrome beginnen bei alpha^0 (fcr=0),
        # die Standard-Forney-Formel ohne diesen Faktor setzt Syndrome ab alpha^1
        # voraus (fcr=1) -> ohne Korrektur waeren nur Fehler mit X_l=1 richtig.
        magnitude = gf.mul(gf.mul(num, gf.inverse(den)), x_l)
        corrected[pos] ^= magnitude

    return corrected


def rs_decode(codeword, r: int, gf: GaloisField):
    """Vollstaendiger Decode-Versuch.
    Rueckgabe: (data_symbols, anzahl_fehler) bei Erfolg, sonst (None, -1).
    """
    n = len(codeword)
    synd = rs_calc_syndromes(codeword, r, gf)
    if all(s == 0 for s in synd):
        return codeword[: n - r], 0

    err_loc = rs_find_error_locator(synd, r, gf)
    if err_loc is None:
        return None, -1

    err_pos = rs_find_errors(err_loc, n, gf)
    if err_pos is None:
        return None, -1

    corrected = rs_correct_errata(codeword, synd, err_pos, gf)
    if corrected is None:
        return None, -1

    # Gegenpruefung: nach Korrektur muessen alle Syndrome 0 sein
    check = rs_calc_syndromes(corrected, r, gf)
    if not all(s == 0 for s in check):
        return None, -1

    return corrected[: n - r], len(err_pos)
