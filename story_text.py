"""Read-only text/DOCX source decoding shared by current producers."""
from pathlib import Path
from xml.etree import ElementTree
import zipfile

def read_trusted_story_text(path: Path) -> str:
    """Read a supported story source without treating DOCX bytes as UTF-8.

    ``story_project.TEXT_EXTENSIONS`` has always admitted DOCX files, so the
    Runtime trust-chain reader must accept the same formats.  This parser is
    deliberately stdlib-only because contract validation runs before optional
    document-production dependencies are needed.
    """

    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8-sig", errors="strict")
    if suffix != ".docx":
        raise ValueError(f"unsupported story contract source format: {path.suffix}")
    try:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
    except (OSError, KeyError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
        raise ValueError(f"invalid DOCX story contract source: {path}") from exc
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        line = "".join(
            node.text or "" for node in paragraph.findall(".//w:t", namespace)
        ).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)
