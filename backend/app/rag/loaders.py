"""Document loaders: HTML/URL, PDF, DOCX, Markdown, CSV -> normalized text sections."""

import csv
import io
import ipaddress
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


@dataclass
class Section:
    text: str
    meta: dict = field(default_factory=dict)  # heading, page, ...


@dataclass
class LoadedDocument:
    title: str
    sections: list[Section]


class UnsupportedFormat(ValueError):
    pass


def load_markdown(data: bytes, filename: str) -> LoadedDocument:
    text = data.decode("utf-8", errors="replace")
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), Path(filename).stem)
    return LoadedDocument(title, [Section(text)])


def load_html(data: bytes | str, fallback_title: str = "") -> LoadedDocument:
    soup = BeautifulSoup(data, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    title = (soup.title.string.strip() if soup.title and soup.title.string else "") or (
        h1.get_text(strip=True) if (h1 := root.find("h1")) else fallback_title
    )
    lines: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr", "pre"]):
        txt = " | ".join(c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])) if el.name == "tr" else el.get_text(" ", strip=True)
        if not txt:
            continue
        if el.name in {"h1", "h2", "h3", "h4"}:
            lines.append(f"\n{'#' * int(el.name[1])} {txt}\n")
        elif el.name == "li":
            lines.append(f"- {txt}")
        else:
            lines.append(txt)
    return LoadedDocument(title or fallback_title, [Section("\n".join(lines).strip())])


def load_pdf(data: bytes, filename: str) -> LoadedDocument:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    sections = [Section(page.extract_text() or "", {"page": i + 1}) for i, page in enumerate(reader.pages)]
    meta_title = (reader.metadata.title if reader.metadata else None) or Path(filename).stem
    return LoadedDocument(meta_title, [s for s in sections if s.text.strip()])


def load_docx(data: bytes, filename: str) -> LoadedDocument:
    import docx

    d = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    for p in d.paragraphs:
        if not p.text.strip():
            continue
        style = (p.style.name or "") if p.style is not None else ""
        lines.append(f"\n## {p.text}\n" if style.lower().startswith("heading") else p.text)
    for table in d.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return LoadedDocument(Path(filename).stem, [Section("\n".join(lines))])


def load_csv(data: bytes, filename: str) -> LoadedDocument:
    """Size tables: every row is rendered with its headers so a chunk stays self-explanatory."""
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return LoadedDocument(Path(filename).stem, [])
    header, body = rows[0], rows[1:]
    stem = Path(filename).stem
    lines = [f"# {stem}", f"Columns: {', '.join(header)}"]
    lines += [f"{stem}: " + "; ".join(f"{h}={v}" for h, v in zip(header, r, strict=False)) for r in body if any(r)]
    return LoadedDocument(stem, [Section("\n".join(lines))])


LOADERS = {
    ".md": load_markdown,
    ".markdown": load_markdown,
    ".txt": load_markdown,
    ".pdf": load_pdf,
    ".docx": load_docx,
    ".csv": load_csv,
    ".html": lambda d, f: load_html(d, Path(f).stem),
    ".htm": lambda d, f: load_html(d, Path(f).stem),
}


def load_file(data: bytes, filename: str) -> LoadedDocument:
    loader = LOADERS.get(Path(filename).suffix.lower())
    if loader is None:
        raise UnsupportedFormat(f"Unsupported file type: {Path(filename).suffix or filename}")
    return loader(data, filename)


def _assert_public_url(url: str) -> None:
    """SSRF guard: only http(s) to public addresses (disabled for the docker-internal mock host via allowlist)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http(s) URLs are allowed")
    for info in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("URL resolves to a non-public address")


def fetch_url(url: str, *, allow_private: bool = False, timeout: float = 20.0) -> tuple[bytes, str]:
    if not allow_private:
        _assert_public_url(url)
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "ShopSupportBot/1.0"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "")


def load_url(url: str, *, allow_private: bool = False) -> tuple[bytes, LoadedDocument]:
    raw, ctype = fetch_url(url, allow_private=allow_private)
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return raw, load_pdf(raw, urlparse(url).path.rsplit("/", 1)[-1] or "document.pdf")
    return raw, load_html(raw, urlparse(url).path.rsplit("/", 1)[-1] or url)
