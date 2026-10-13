"""
Gemeinsame Encode-Pipeline fuer encode.py und reframe.py:
Bytes -> Symbole -> Layout -> Nutzdatenzeilen (RS + CRC) -> Seiten mit Header.
"""
import hashlib
import os
import zlib
from dataclasses import dataclass

from .alphabets import Alphabet
from .framing import Layout, arrange_columns, build_payload_line, byte_offset_of_line, compute_layout
from .header import DOCUMENT_ID_WIDTH, build_header_lines, pack_header
from .progress import ProgressBar
from .rs_codec import GaloisField, rs_generator_poly
from .scramble import scramble_block
from .util import ceil_div

BOM = "﻿"
PAGE_SEPARATOR = "\n\n\n"  # 3 Leerzeilen zwischen Seiten in einem gemeinsamen Textstrom


@dataclass(frozen=True)
class EncodedPage:
    preamble: str
    header_lines: list[str]
    payload_lines: list[str]


def plan_layout(data: bytes, width: int, payload_alpha: Alphabet, header_alpha: Alphabet,
                redundancy_pct: float | None, parity_symbols: int | None) -> Layout:
    """Berechnet das Zeilenlayout (k, r, Zeilennummernbreite, ...) fuer `data`."""
    total_data_symbols = ceil_div(len(data) * 8, payload_alpha.bits_per_symbol)
    return compute_layout(
        width=width, total_data_symbols=total_data_symbols,
        payload_alpha=payload_alpha, header_alpha=header_alpha,
        redundancy_pct=redundancy_pct, parity_symbols=parity_symbols,
    )


def encode_document(data: bytes, layout: Layout, width: int, payload_alpha: Alphabet,
                    header_alpha: Alphabet, is_custom_alphabet: bool, text_encoding: int,
                    filename: str, scrambled: bool, lines_per_page: int | None = None,
                    progress: bool = False, progress_label: str = "Encoding") -> list[EncodedPage]:
    """Kodiert `data` vollstaendig. lines_per_page=None: eine einzige Seite.
    Rueckgabe: Liste der Seiten (jede mit eigener Praeambel und Header-Kopie)."""
    k, r = layout.k, layout.r
    total_lines = layout.total_lines
    bits_per_symbol = payload_alpha.bits_per_symbol
    gf = GaloisField(bits_per_symbol)
    gen = rs_generator_poly(r, gf)

    # Nutzdaten in Bloecke aufteilen (letzter Block mit 0 aufgefuellt)
    data_symbols = payload_alpha.bytes_to_symbols(data)
    data_symbols += [0] * (total_lines * k - len(data_symbols))

    # Dokument-ID schon hier, da sie den Verwuerfelungs-Schluesselstrom bestimmt
    document_id = os.urandom(DOCUMENT_ID_WIDTH)

    payload_lines = []
    bar = ProgressBar(total=total_lines, enabled=progress, label=progress_label)
    for i in range(total_lines):
        block = data_symbols[i * k:(i + 1) * k]
        if scrambled:
            block = scramble_block(block, i, document_id, bits_per_symbol)
        payload_lines.append(
            build_payload_line(i, block, k, r, gen, gf, layout.ln_width, layout.crc_width,
                               payload_alpha, header_alpha)
        )
        bar.update(i + 1)
    bar.done()

    sha256 = hashlib.sha256(data).digest()
    lines_per_page = lines_per_page or total_lines
    page_count = ceil_div(total_lines, lines_per_page)

    pages = []
    for page_number in range(page_count):
        start = page_number * lines_per_page
        end = min(start + lines_per_page, total_lines)
        byte_start = byte_offset_of_line(start, k, bits_per_symbol)
        byte_end = min(byte_offset_of_line(end, k, bits_per_symbol), len(data))
        header_bytes = pack_header(
            payload_alpha, is_custom_alphabet, k, r, len(data), sha256, layout.ln_width, filename,
            document_id=document_id, page_count=page_count, page_number=page_number,
            page_checksum=zlib.crc32(data[byte_start:byte_end]), text_encoding=text_encoding,
            scrambled=scrambled,
        )
        preamble, header_lines = build_header_lines(header_bytes, width, header_alpha)
        pages.append(EncodedPage(preamble, header_lines, payload_lines[start:end]))
    return pages


def write_page(out, page: EncodedPage, columns: int = 1, col_width: int = 0, write_bom: bool = False):
    """Schreibt eine Seite in den Textstrom `out`. Die Nutzdatenzeilen werden
    bei columns > 1 spaltenweise angeordnet (siehe framing.arrange_columns),
    Praeambel und Header bleiben einspaltig."""
    if write_bom:
        out.write(BOM)
    out.write(page.preamble + "\n")
    for line in page.header_lines:
        out.write(line + "\n")
    for line in arrange_columns(page.payload_lines, columns, col_width):
        out.write(line + "\n")
