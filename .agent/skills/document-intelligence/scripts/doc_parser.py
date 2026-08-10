"""
Document Parser — Extracts text from various file formats.
Supports: PDF, DOCX, TXT, Markdown, Google Docs (exported as text).
Designed to be extensible (XLSX/PPTX can be added later).
"""
import io
import os
import sys
import tempfile


def parse_document(content, mime_type, filename=""):
    """Parse document content and return extracted text.

    Args:
        content: bytes or str (raw file content)
        mime_type: the MIME type of the document
        filename: original filename (used for format detection fallback)

    Returns:
        (text, metadata, error)
        text: extracted plain text
        metadata: dict with page_count, word_count, etc.
        error: error message or None
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ""

    try:
        if mime_type == "application/pdf" or ext == ".pdf":
            return _parse_pdf(content)
        elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or ext == ".docx":
            return _parse_docx(content)
        elif mime_type in ("text/plain", "text/markdown") or ext in (".txt", ".md"):
            return _parse_text(content)
        elif mime_type == "application/vnd.google-apps.document":
            # Google Docs are already exported as text by the connector
            return _parse_text(content)
        else:
            return None, {}, f"Unsupported format: {mime_type} ({filename})"
    except Exception as e:
        return None, {}, f"Parse error ({filename}): {e}"


def _parse_text(content):
    """Parse plain text / markdown."""
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = str(content)
    words = len(text.split())
    return text, {"word_count": words, "format": "text"}, None


def _parse_pdf(content):
    """Parse PDF using PyPDF2 (if available) or pdfplumber."""
    if isinstance(content, str):
        content = content.encode("utf-8")

    # Try PyPDF2 first (lighter)
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        full_text = "\n\n".join(pages)
        return full_text, {"page_count": len(reader.pages), "word_count": len(full_text.split()), "format": "pdf"}, None
    except ImportError:
        pass

    # Try pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            full_text = "\n\n".join(pages)
            return full_text, {"page_count": len(pdf.pages), "word_count": len(full_text.split()), "format": "pdf"}, None
    except ImportError:
        return None, {}, "PDF parsing requires PyPDF2 or pdfplumber. Install: pip install PyPDF2"


def _parse_docx(content):
    """Parse DOCX using python-docx."""
    if isinstance(content, str):
        content = content.encode("utf-8")

    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        full_text = "\n\n".join(paragraphs)
        return full_text, {"paragraph_count": len(paragraphs), "word_count": len(full_text.split()), "format": "docx"}, None
    except ImportError:
        return None, {}, "DOCX parsing requires python-docx. Install: pip install python-docx"
