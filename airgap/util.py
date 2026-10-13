"""Kleine Ganzzahl-Hilfsfunktionen, die von mehreren Modulen gebraucht werden."""


def ceil_div(a: int, b: int) -> int:
    """Ganzzahlige Division mit Aufrunden (exakt auch fuer sehr grosse Werte,
    anders als math.ceil(a / b) ueber float)."""
    return -(-a // b)


def int_to_digits(value: int, width: int, base: int) -> list[int]:
    """Stellt `value` als genau `width` Ziffern zur Basis `base` dar
    (hoechstwertige Ziffer zuerst). Wirft ValueError, wenn der Wert nicht passt."""
    if value < 0:
        raise ValueError(f"Wert {value} ist negativ")
    digits = []
    v = value
    for _ in range(width):
        v, d = divmod(v, base)
        digits.append(d)
    if v != 0:
        raise ValueError(f"Wert {value} passt nicht in {width} Symbole (Basis {base})")
    digits.reverse()
    return digits


def digits_to_int(digits, base: int) -> int:
    """Umkehrung von int_to_digits."""
    v = 0
    for d in digits:
        v = v * base + d
    return v


def digits_needed(count: int, base: int) -> int:
    """Anzahl Ziffern zur Basis `base`, um die Werte 0..count-1 darzustellen
    (mindestens 1)."""
    width = 1
    limit = base
    while count > limit:
        width += 1
        limit *= base
    return width
