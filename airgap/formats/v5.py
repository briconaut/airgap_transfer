"""Formatversion 5 = 4 + Zeilen-CRC ueber das VOLLSTAENDIGE Symbol bei
Alphabeten mit mehr als 8 Bit/Symbol (GF512, 2 Byte/Symbol, siehe
framing.symbols_crc32). Fuer Alphabete <= 256 Zeichen bit-identisch zu 4."""
from .v4 import FormatV4


class FormatV5(FormatV4):
    version = 5

    def crc_is_wide(self, bits_per_symbol: int) -> bool:
        return bits_per_symbol > 8
