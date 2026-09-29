import io
import logging

from .capitec import CapitecParser
from .csv_parser import TEMPLATE as CSV_TEMPLATE, parse_csv
from .generic import GenericParser
from .gotyme import GoTymeParser, is_gotyme
from .tymebank import TymeBankLegacyParser

log = logging.getLogger(__name__)

__all__ = ["parse_pdf", "parse_csv", "extract_text", "CSV_TEMPLATE"]


def extract_text(pdf_bytes, password=None):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        if not password:
            raise ValueError("PDF is password protected but no password provided")
        if not reader.decrypt(password):
            raise ValueError("Incorrect PDF password")
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_pdf(pdf_bytes, bank_name, password=None):
    """Returns a list of {date, description, amount, type, reference, [category, fee, balance]}."""
    text = extract_text(pdf_bytes, password)
    # GoTyme is auto-detected whatever bank the user picked.
    if bank_name == "gotyme" or is_gotyme(text):
        return GoTymeParser().parse(pdf_bytes, password)
    if bank_name == "tymebank":
        return TymeBankLegacyParser().parse(text)
    if bank_name == "capitec":
        return CapitecParser().parse(text)
    return GenericParser().parse(text)
