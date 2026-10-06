#!/usr/bin/env python3
"""
Parse saved pages from the Competition Tribunal website.

Matter pages (/current-matters/<slug>) publish their filings as a simple HTML
table:

    | Date filed | Filed by | Description (link) | Confidentiality |

parse_documents() turns that table into a list of dicts with only the keys
that are available on the page, plus a repo-relative ``url_gh`` pointing at
where the downloaded file lives:

    {
      "date": "2026-07-21",
      "filed_by": "-",
      "description": "Directions",
      "confidentiality": "Non-confidential",
      "url": "https://www.competitiontribunal.gov.au/__data/assets/pdf_file/0020/600284/Directions.pdf",
      "url_gh": "/matters/act-1-of-2026/documents/600284-Directions.pdf"
    }

The /current-matters page lists open matters as links in the first list under
the "Current matters" heading (concluded matters follow under their own <h2>).
parse_current_matters() returns them as:

    {
      "number": "ACT 1 of 2026",
      "title": "Application by Coles Supermarkets Australia Pty Limited",
      "url": "https://www.competitiontribunal.gov.au/current-matters/act-1-of-2026"
    }

Run standalone to parse a saved HTML file and print the JSON:

    parse.py documents page.html [DOCS_DIR]
    parse.py current-matters page.html
"""

import json
import re
import sys
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

BASE_URL = "https://www.competitiontribunal.gov.au"
DEFAULT_DOCS_DIR = "documents"


def normalize_date(text: str) -> str:
    """'21 July 2026' -> '2026-07-21'; leave unrecognised text untouched."""
    text = text.strip()
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def document_filename(url: str) -> str:
    """Local filename for a document URL.

    Asset URLs look like /__data/assets/pdf_file/0020/600284/Directions.pdf.
    The bare filename isn't unique within a matter (there are plenty of
    "Directions.pdf"s), so prefix it with the numeric asset id."""
    parts = url.rstrip("/").split("/")
    filename = parts[-1]
    if len(parts) >= 2 and parts[-2].isdigit():
        filename = f"{parts[-2]}-{filename}"
    return filename


class _DocTableParser(HTMLParser):
    """Collect the rows of every bordered table on the page.

    Each cell becomes ``{"text": ..., "href": ...}`` where ``text`` excludes
    the ``<em>(PDF, 231.0 KB)</em>`` size hint so the description comes out
    clean, and ``href`` is the first link in the cell (or None)."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[dict]] = []
        self.tables = 0
        self._in_table = False
        self._in_cell = False
        self._in_em = False
        self._row: list[dict] | None = None
        self._row_has_th = False
        self._cell_text: list[str] = []
        self._cell_href: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            if "table-bordered" in (attrs.get("class") or ""):
                self._in_table = True
                self.tables += 1
        elif self._in_table and tag == "tr":
            self._row = []
            self._row_has_th = False
        elif self._row is not None and tag in ("td", "th"):
            self._in_cell = True
            self._cell_text = []
            self._cell_href = None
            if tag == "th":
                self._row_has_th = True
        elif self._in_cell and tag == "a" and self._cell_href is None:
            self._cell_href = attrs.get("href")
        elif self._in_cell and tag == "em":
            self._in_em = True

    def handle_endtag(self, tag):
        if tag == "table" and self._in_table:
            self._in_table = False
        elif tag == "tr" and self._row is not None:
            if self._row and not self._row_has_th:
                self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            text = clean("".join(self._cell_text))
            if self._row is not None:
                self._row.append({"text": text, "href": self._cell_href})
        elif tag == "em" and self._in_em:
            self._in_em = False

    def handle_data(self, data):
        if self._in_cell and not self._in_em:
            self._cell_text.append(data)


def has_documents_table(html: str) -> bool:
    parser = _DocTableParser()
    parser.feed(html)
    return parser.tables > 0


def parse_documents(html: str, docs_dir: str = DEFAULT_DOCS_DIR) -> list[dict]:
    parser = _DocTableParser()
    parser.feed(html)

    documents = []
    for row in parser.rows:
        if len(row) < 4:
            continue
        url = row[2]["href"]
        if not url:
            continue
        url = urljoin(BASE_URL + "/", url)
        documents.append(
            {
                "date": normalize_date(row[0]["text"]),
                "filed_by": row[1]["text"],
                "description": row[2]["text"],
                "confidentiality": row[3]["text"],
                "url": url,
                "url_gh": f"/{docs_dir}/{document_filename(url)}",
            }
        )
    return documents


def _first_tag_text(html: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.I | re.S)
    return clean(re.sub(r"<[^>]+>", "", m.group(1))) if m else ""


def parse_matter_heading(html: str) -> dict:
    """The matter number (<h1>, e.g. 'ACT 1 of 2026') and title (first <h2>)."""
    return {"number": _first_tag_text(html, "h1"), "title": _first_tag_text(html, "h2")}


def parse_current_matters(html: str) -> list[dict]:
    """Links between the 'Current matters' <h1> and the next <h2> (which
    starts the concluded matters)."""
    m = re.search(r"<h1[^>]*>\s*Current matters\s*</h1>(.*?)(?:<h2|$)", html, re.I | re.S)
    if not m:
        return []
    matters = []
    for href, text in re.findall(
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', m.group(1), re.I | re.S
    ):
        text = clean(re.sub(r"<[^>]+>", "", text))
        # "ACT 1 of 2026 - Application by ..." (sometimes an en dash)
        number, title = (re.split(r"\s+[-–—]\s+", text, maxsplit=1) + [""])[:2]
        matters.append(
            {
                "number": number.strip(),
                "title": title.strip(),
                "url": urljoin(BASE_URL + "/", href),
            }
        )
    return matters


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("documents", "current-matters"):
        print(
            "Usage: parse.py documents page.html [DOCS_DIR]\n"
            "       parse.py current-matters page.html",
            file=sys.stderr,
        )
        return 1
    with open(sys.argv[2], encoding="utf-8") as f:
        html = f.read()
    if sys.argv[1] == "documents":
        docs_dir = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_DOCS_DIR
        out = {
            "matter": parse_matter_heading(html),
            "documents": parse_documents(html, docs_dir=docs_dir),
        }
    else:
        out = {"current_matters": parse_current_matters(html)}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
