import io
import logging

from .capitec import CapitecParser
from .csv_parser import TEMPLATE as CSV_TEMPLATE, parse_csv
from .generic import GenericParser
from .gotyme import GoTymeParser, is_gotyme
from .tymebank import TymeBankLegacyParser

log = logging.getLogger(__name__)

__all__ = ["parse_pdf", "parse_csv", "parse_text", "extract_text", "open_pdf", "CSV_TEMPLATE"]


def open_pdf(pdf_bytes, passwords=()):
    """(PdfReader, password that worked). Tries no password, blank, then each saved password."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if not reader.is_encrypted:
        return reader, None
    for pw in ("", *[p for p in passwords if p]):
        try:
            if reader.decrypt(pw):
                return reader, pw
        except Exception:
            continue
    if not any(passwords):
        raise ValueError("PDF is password protected but no password provided")
    raise ValueError("Incorrect PDF password: none of the saved passwords opened it")


def extract_text(pdf_bytes, password=None, passwords=()):
    reader, _ = open_pdf(pdf_bytes, (password, *passwords))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


TEXT_PARSERS = {"tymebank": TymeBankLegacyParser, "capitec": CapitecParser, "generic": GenericParser}


def parse_text(text, bank_name):
    """The bank's own parser first; when it finds nothing, whichever other text parser finds the most."""
    first = TEXT_PARSERS.get(bank_name, GenericParser)().parse(text)
    if first:
        return first
    best = []
    for name, parser in TEXT_PARSERS.items():
        if name != bank_name:
            rows = parser().parse(text)
            if len(rows) > len(best):
                best = rows
    return best


def parse_pdf(pdf_bytes, bank_name, password=None, passwords=()):
    """Returns a list of {date, description, amount, type, reference, [category, fee, balance]}."""
    reader, pw = open_pdf(pdf_bytes, (password, *passwords))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    # GoTyme is auto-detected whatever bank the user picked.
    if bank_name == "gotyme" or is_gotyme(text):
        rows = GoTymeParser().parse(pdf_bytes, pw)
        if rows:
            return rows
    return parse_text(text, bank_name)
