"""Transaction parsers package."""
try:
    from gopay_pdf import parse_gopay_pdf, PARSER_VERSION
except ImportError:
    from parsers.gopay_pdf import parse_gopay_pdf, PARSER_VERSION

__all__ = ['parse_gopay_pdf', 'PARSER_VERSION']
