"""
Registry der Formatversionen (Bitformat von Header und Nutzdatenzeilen).

Jede Version ist eine Klasse in einem eigenen Modul, linear von ihrer
Vorgaengerin abgeleitet (siehe base.py). Encoder schreiben jede hier
registrierte Version, der Decoder liest sie alle (Auswahl ueber das
magic-Byte des Headers). Neue Version: Modul vN.py + Eintrag in FORMATS.
"""
from ..header import HeaderError, HeaderMeta
from .base import Format
from .v3 import FormatV3
from .v4 import FormatV4
from .v5 import FormatV5
from .v6 import FormatV6

FORMATS: dict[int, Format] = {f.version: f for f in (FormatV3(), FormatV4(), FormatV5(), FormatV6())}
CURRENT_FORMAT_VERSION = max(FORMATS)
SUPPORTED_FORMAT_VERSIONS = tuple(sorted(FORMATS))


def get_format(version: int = CURRENT_FORMAT_VERSION) -> Format:
    try:
        return FORMATS[version]
    except KeyError:
        raise HeaderError(
            f"Formatversion {version} wird von diesem Programm nicht unterstuetzt "
            f"(bekannt: {SUPPORTED_FORMAT_VERSIONS})."
        ) from None


def unpack_header(data: bytes) -> HeaderMeta:
    """Entpackt Header-Rohbytes beliebiger unterstuetzter Formatversion."""
    if not data:
        raise HeaderError("Header-Rohbytes zu kurz/beschaedigt")
    return get_format(data[0]).unpack_header(data)
