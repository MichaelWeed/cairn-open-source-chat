import io

from pypdf import PdfReader

SUPPORTED_EXTENSIONS = frozenset({".md", ".markdown", ".pdf"})


class UnsupportedFileType(ValueError):
    pass


def extract_text(filename: str, content: bytes) -> str:
    suffix = _suffix(filename)
    if suffix in (".md", ".markdown"):
        return content.decode("utf-8")
    if suffix == ".pdf":
        return _extract_pdf_text(content)
    raise UnsupportedFileType(f"unsupported file type: {suffix or filename!r}")


def _suffix(filename: str) -> str:
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[1].lower()


def _extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()
