import re
from html import unescape
from typing import List

try:  # pragma: no cover - optional dependency
    from weasyprint import HTML
except Exception:  # pragma: no cover
    HTML = None


def _escape_pdf_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def can_render_html_pdf() -> bool:
    return HTML is not None


def build_simple_pdf(title: str, lines: List[str]) -> bytes:
    """
    Generates a very small PDF with the given title and lines.
    It is not pretty, but enough for email attachments without external deps.
    """

    text_lines = [title] + lines
    y = 790
    content_parts = []

    for line in text_lines:
        safe_line = _escape_pdf_text(line[:200])
        content_parts.append(
            f"BT /F1 11 Tf 40 {y} Td ({safe_line}) Tj ET"
        )
        y -= 16

    content_stream = "\n".join(content_parts).encode("latin-1", "ignore")
    catalog = "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    pages = "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    page = (
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
    )
    contents = (
        f"4 0 obj << /Length {len(content_stream)} >> stream\n".encode("ascii")
        + content_stream
        + b"\nendstream\nendobj\n"
    )
    font = "5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"

    parts = [b"%PDF-1.4\n"]
    xref_positions = []

    for obj in [catalog, pages, page]:
        xref_positions.append(sum(len(part) for part in parts))
        parts.append(obj.encode("ascii"))

    xref_positions.append(sum(len(part) for part in parts))
    parts.append(contents)

    xref_positions.append(sum(len(part) for part in parts))
    parts.append(font.encode("ascii"))

    xref_start = sum(len(part) for part in parts)
    parts.append(f"xref\n0 6\n0000000000 65535 f \n".encode("ascii"))
    for pos in xref_positions:
        parts.append(f"{pos:010} 00000 n \n".encode("ascii"))

    parts.append(
        b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_start).encode("ascii")
        + b"\n%%EOF"
    )

    return b"".join(parts)


STYLE_PATCH = """
@media print {
  .summary {
    display: flex !important;
    flex-wrap: nowrap !important;
    gap: 8px !important;
    padding-top: 10px !important;
    padding-bottom: 6px !important;
  }
  .summary .card {
    flex: 1 1 0 !important;
    width: auto !important;
    min-width: 0 !important;
    padding: 10px 12px !important;
    border-radius: 8px !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: center !important;
    min-height: 80px !important;
  }
  .summary .card .label {
    margin-bottom: 4px !important;
    font-size: 10px !important;
  }
  .summary .card .value {
    font-size: 18px !important;
  }
  header,
  .meta,
  .summary,
  .table-block,
  .note,
  .components {
    page-break-inside: avoid !important;
  }
}
"""


def _inject_style_patch(html: str) -> str:
    if not html:
        return html

    injection = f"<style>{STYLE_PATCH}</style>"
    head_close_pattern = re.compile(
        r"</head>",
        flags=re.IGNORECASE,
    )
    if head_close_pattern.search(html):
        return head_close_pattern.sub(injection + "</head>", html, count=1)

    return (
        "<!DOCTYPE html><html><head>"
        + injection
        + "</head><body>"
        + html
        + "</body></html>"
    )


def build_pdf_from_html(title: str, html_content: str) -> bytes:
    if HTML is not None:
        html_with_patch = _inject_style_patch(html_content or "")
        document = HTML(string=html_with_patch)
        return document.write_pdf(stylesheets=None)

    fallback_source = html_content or ""
    fallback_source = re.sub(
        r"<style\b[^>]*>[\s\S]*?</style>",
        " ",
        fallback_source,
        flags=re.IGNORECASE,
    )
    fallback_source = re.sub(
        r"<script\b[^>]*>[\s\S]*?</script>",
        " ",
        fallback_source,
        flags=re.IGNORECASE,
    )
    plain_text = re.sub(r"<[^>]+>", " ", fallback_source)
    plain_text = unescape(plain_text).replace("\xa0", " ")
    plain_text = re.sub(r"[ \t]+", " ", plain_text)
    lines = [line.strip() for line in plain_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        lines = ["Reporte generado desde Kensar."]
    return build_simple_pdf(title or "Reporte Kensar", lines)
