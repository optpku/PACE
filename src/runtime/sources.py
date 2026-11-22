"""Acquire Survey files and persist resumable, non-authoritative source context."""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tarfile
import urllib.request
import zipfile
import zlib
from bisect import bisect_right
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from pace.artifacts.workspace import _write_json_atomic, path_has_symlink_component


def _public_url(url):
    value = urlparse(url)
    if (
        value.scheme not in {"http", "https"}
        or not value.hostname
        or value.username
        or value.password
    ):
        raise ValueError("only public HTTP(S) URLs are permitted")
    for item in socket.getaddrinfo(
        value.hostname, value.port or (443 if value.scheme == "https" else 80)
    ):
        if not ipaddress.ip_address(item[4][0]).is_global:
            raise ValueError("local or private network address is not a source")
    return url


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(
            req, fp, code, msg, headers, _public_url(newurl)
        )


DEFAULT_READ_CHARS = 16000  # Response page size, not a file acceptance limit.


def _omit_binary_urls(text):
    return re.sub(
        r"data:(?:font/|image/|application/(?:font|x-font|vnd\.ms-fontobject|octet-stream))"
        r"[^\s\"\'<>)]*",
        "[embedded binary resource omitted]",
        text,
        flags=re.I,
    )


def _html_document(text, path, content_type=""):
    return (
        content_type in {"text/html", "application/xhtml+xml", "image/svg+xml"}
        or path.suffix.lower() in {".html", ".htm", ".xhtml", ".svg"}
        or bool(
            re.match(
                r"\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*"
                r"<(?:!doctype\s+html\b|html\b|head\b|body\b|svg\b)",
                text,
                re.I | re.S,
            )
        )
    )


def _source_view(path, view="text", source_info=None):
    """Build a reading view without changing the original or executing page code."""
    if view not in {"text", "embedded"}:
        raise ValueError("view must be text or embedded")
    path = Path(path)
    # Callers supply authorized metadata; never read a neighboring file outside
    # a role's input view, especially for an independent blind probe.
    source_info = source_info or {}
    encoding = source_info.get("charset") or "utf-8"
    base_url = source_info.get("effective_url", "")
    content_type = source_info.get("content_type", "")
    payload = path.read_bytes()
    prefix = payload[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n\f ")
    meta = {
        "view": view,
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "available_views": ["text"],
        "source_format": "TEXT",
    }
    if prefix.startswith(b"%PDF-"):
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError

        try:
            reader = PdfReader(path)
            text = "\f".join(page.extract_text() or "" for page in reader.pages)
            meta.update(source_format="PDF", total_pages=len(reader.pages))
        except PyPdfError as exc:
            raise ValueError(f"PDF text extraction failed: {exc}") from exc
    elif (
        path.suffix.lower()
        in {
            ".css",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
            ".eot",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".ico",
        }
        or content_type == "text/css"
        or content_type.startswith("font/")
        or prefix.startswith(
            (
                b"wOFF",
                b"wOF2",
                b"OTTO",
                b"\x00\x01\x00\x00",
                b"\x89PNG",
                b"\xff\xd8\xff",
                b"GIF8",
            )
        )
    ):
        return "", {
            **meta,
            "source_format": "RESOURCE",
            "reading_note": "Presentation or binary resource omitted from the reading view. Original bytes remain on disk.",
        }
    else:
        text = payload.decode(encoding)
        if _html_document(text, path, content_type):
            parser = _HTMLText(base_url)
            parser.feed(text)
            parser.close()
            text = parser.reading_text() if view == "text" else parser.embedded_text()
            meta.update(
                source_format="HTML",
                available_views=["text", "embedded"],
                embedded_blocks=len(parser.scripts),
                reading_note="Coordinates refer to this reading view, not raw HTML lines. "
                "CSS, fonts and binary assets are excluded; original bytes remain on disk. "
                "Use search_file with view=embedded to locate dynamic-page data or script URLs; no JavaScript is executed.",
            )
        elif (
            path.suffix.lower() in {".js", ".mjs", ".cjs"}
            or "javascript" in content_type
        ):
            text = _omit_binary_urls(text)
            meta.update(
                source_format="JAVASCRIPT",
                available_views=["text", "embedded"],
                reading_note="Source code is not executed. Search for the required data instead of reading the whole program.",
            )
    return text, meta


def read_source(
    path,
    *,
    start_line=1,
    end_line=None,
    start_page=None,
    end_page=None,
    start_char=None,
    end_char=None,
    max_chars: int | None = DEFAULT_READ_CHARS,
    view="text",
    source_info=None,
):
    """Page a content-aware view. Every character remains reachable by cursor."""
    text, meta = _source_view(path, view, source_info)
    if max_chars is not None and (type(max_chars) is not int or max_chars < 1):
        raise ValueError("max_chars must be a positive response page size")
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if start_char is not None or end_char is not None:
        if (
            start_line != 1
            or end_line is not None
            or start_page is not None
            or end_page is not None
        ):
            raise ValueError("use character, line, or page coordinates separately")
        begin, end = (
            0 if start_char is None else start_char,
            len(text) if end_char is None else end_char,
        )
        if type(begin) is not int or type(end) is not int or not 0 <= begin <= end:
            raise ValueError(
                "invalid character range; offsets are zero-based, end exclusive"
            )
    elif start_page is not None or end_page is not None:
        if start_line != 1 or end_line is not None:
            raise ValueError("use page or line coordinates separately")
        pages = text.split("\f")
        first, last = (
            1 if start_page is None else start_page,
            len(pages) if end_page is None else end_page,
        )
        if (
            type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last <= len(pages)
        ):
            raise ValueError("invalid page range")
        begin = sum(len(page) + 1 for page in pages[: first - 1])
        end = sum(len(page) + 1 for page in pages[:last]) - 1
        meta.update(
            total_pages=len(pages),
            start_page=first,
            end_page=last,
            next_start_page=last + 1 if last < len(pages) else None,
        )
    else:
        last = max(start_line, len(lines)) if end_line is None else end_line
        if (
            type(start_line) is not int
            or type(last) is not int
            or not 1 <= start_line <= last
        ):
            raise ValueError("invalid line range")
        begin, end = (
            offsets[min(start_line - 1, len(lines))],
            offsets[min(last, len(lines))],
        )
    begin, end = min(begin, len(text)), min(end, len(text))
    stop = end if max_chars is None else min(end, begin + max_chars)
    first_line = min(bisect_right(offsets, begin), len(lines)) if lines else 1
    last_line = (
        min(bisect_right(offsets, max(begin, stop - 1)), len(lines)) if lines else 0
    )
    next_read = None
    if stop < len(text):
        next_read = {"view": view, "start_char": stop}
        if max_chars is not None:
            next_read["max_chars"] = max_chars
        if stop < end:
            next_read["end_char"] = end
    return {
        **meta,
        "content": text[begin:stop],
        "total_chars": len(text),
        "start_char": begin,
        "end_char": stop,
        "selection_end_char": end,
        "selection_complete": stop == end,
        "next_start_char": stop if next_read else None,
        "next_read": next_read,
        "start_line": first_line,
        "end_line": last_line,
        "total_lines": len(lines),
        "next_start_line": bisect_right(offsets, stop) if next_read else None,
    }


def search_source(
    path,
    query,
    *,
    view="text",
    start_char=0,
    max_results=20,
    context_chars=200,
    case_sensitive=False,
    source_info=None,
):
    """Locate literal text, including embedded page data, without returning whole lines."""
    if not isinstance(query, str) or not query:
        raise ValueError("query must be nonempty literal text")
    if (
        type(start_char) is not int
        or start_char < 0
        or type(max_results) is not int
        or max_results < 1
        or type(context_chars) is not int
        or context_chars < 0
        or type(case_sensitive) is not bool
    ):
        raise ValueError("invalid search pagination options")
    text, meta = _source_view(path, view, source_info)
    rows, next_start = [], None
    pattern = re.compile(re.escape(query), 0 if case_sensitive else re.I)
    for match in pattern.finditer(text, start_char):
        if len(rows) == max_results:
            next_start = match.start()
            break
        begin, end = (
            max(0, match.start() - context_chars),
            min(len(text), match.end() + context_chars),
        )
        rows.append(
            {
                "match_start_char": match.start(),
                "match_end_char": match.end(),
                "start_char": begin,
                "end_char": end,
                "content": text[begin:end],
                "read_args": {"view": view, "start_char": begin, "end_char": end},
            }
        )
    return {
        **meta,
        "query": query,
        "matches": rows,
        "total_chars": len(text),
        "next_start_char": next_start,
        "next_search": (
            {
                "query": query,
                "view": view,
                "start_char": next_start,
                "max_results": max_results,
                "context_chars": context_chars,
                "case_sensitive": case_sensitive,
            }
            if next_start is not None
            else None
        ),
    }


class _HTMLText(HTMLParser):
    """Keep prose, source links and TeX math alternatives in the reading copy."""

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.skip = [], [], []
        self.base_url = base_url
        self.math_script = False
        self.raw_math = False
        self.scripts = []
        self.script = None

    def reading_text(self):
        return re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", "".join(self.parts))

    def embedded_text(self):
        return "\n\n".join(
            f"[script {i}; type={item['type']}; src={item['src']}]\n"
            + _omit_binary_urls("".join(item["parts"]))
            for i, item in enumerate(self.scripts, 1)
        )

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.raw_math:
            from html import escape

            self.parts.append(
                "<"
                + tag
                + "".join(
                    f' {key}="{escape(value, quote=True)}"'
                    for key, value in attrs.items()
                    if value is not None
                    and key not in {"style", "src"}
                    and not value.lower().startswith("data:")
                )
                + ">"
            )
            return
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.append(tag)
            return
        if tag == "script" and (attrs.get("type") or "").startswith("math/tex"):
            self.math_script = True
            self.parts.append("\\(")
        elif tag == "script":
            self.script = {
                "type": attrs.get("type") or "javascript",
                "src": urljoin(self.base_url, attrs.get("src", ""))
                if attrs.get("src")
                else "",
                "parts": [],
            }
            if self.script["src"].lower().startswith("data:"):
                self.script["src"] = "[embedded script URL omitted]"
            self.scripts.append(self.script)
        elif tag == "style":
            self.skip.append(tag)
        elif tag == "math" and (alttext := attrs.get("alttext")):
            self.parts.append("\\(" + alttext + "\\)")
            self.skip.append(tag)
        elif tag == "math":
            self.raw_math = True
            self.parts.append(self.get_starttag_text())
        elif tag in {"sup", "sub"}:
            self.parts.append("^{" if tag == "sup" else "_{")
        elif tag == "a":
            href = attrs.get("href") or ""
            self.links.append(
                ""
                if href.lower().startswith(("data:", "javascript:"))
                else urljoin(self.base_url, href)
            )
        elif tag == "img" and attrs.get("alt"):
            self.parts.append(attrs["alt"])
        elif tag in {
            "p",
            "div",
            "section",
            "article",
            "header",
            "footer",
            "li",
            "tr",
            "br",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "pre",
        }:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if self.raw_math:
            self.parts.append("</" + tag + ">")
            if tag == "math":
                self.raw_math = False
            return
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == "script" and self.math_script:
            self.parts.append("\\)")
            self.math_script = False
        elif tag == "script":
            self.script = None
        elif tag == "a" and self.links:
            url = self.links.pop()
            if url:
                self.parts.append(" <" + url + ">")
        elif tag in {"sup", "sub"}:
            self.parts.append("}")
        elif tag in {
            "p",
            "div",
            "section",
            "article",
            "header",
            "footer",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "pre",
        }:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.script is not None:
            self.script["parts"].append(data)
        else:
            self.parts.append(data)


def fetch_source(
    root,
    cache,
    url,
    *,
    start_line=1,
    end_line=None,
    start_page=None,
    end_page=None,
    start_char=None,
    end_char=None,
    max_chars: int | None = DEFAULT_READ_CHARS,
    view="text",
    full_text=False,
    timeout=None,
):
    """Download once, retain exact bytes, and return a navigable reading view.

    Pagination controls the response only, never whether the source is retained.
    A DOWNLOADED receipt survives an extraction failure and avoids refetching.
    """
    root, cache = Path(root).resolve(), Path(cache)
    folder = cache / hashlib.sha256(url.encode()).hexdigest()
    if not folder.resolve().is_relative_to(root) or path_has_symlink_component(
        root, folder
    ):
        raise ValueError("unsafe source cache path")
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / "source.json"
    if receipt.is_file():
        record = json.loads(receipt.read_text())
        if record["requested_url"] != url:
            raise ValueError("source cache URL mismatch")
        artifact = root / record["artifact_ref"]
        if (
            path_has_symlink_component(root, artifact)
            or not artifact.resolve().is_relative_to(root)
            or hashlib.sha256(artifact.read_bytes()).hexdigest() != record["sha256"]
        ):
            raise ValueError("cached source changed; retain the original version")
    else:
        request = urllib.request.Request(
            _public_url(url), headers={"User-Agent": "PACE-Survey/0.1"}
        )
        temporary = folder / "download.part"
        try:
            with urllib.request.build_opener(_PublicRedirect).open(
                request, timeout=timeout
            ) as response:
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                effective_url = response.geturl()
                with temporary.open("wb") as stream:
                    shutil.copyfileobj(response, stream, length=1024 * 1024)
            artifact = folder / "original"
            temporary.replace(artifact)
        finally:
            temporary.unlink(missing_ok=True)
        record = {
            "requested_url": url,
            "effective_url": effective_url,
            "content_type": content_type,
            "charset": charset,
            "artifact_ref": artifact.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "download_bytes": artifact.stat().st_size,
            "status": "DOWNLOADED",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "authority": "SEARCH_CONTEXT_ONLY",
        }
        _write_json_atomic(receipt, record)
    if record["status"] != "READY":
        try:
            paths, kind = _extract_text(
                artifact,
                text_index=False,
                encoding=record["charset"],
                base_url=record["effective_url"],
            )
        except (
            OSError,
            ValueError,
            EOFError,
            LookupError,
            tarfile.TarError,
            zipfile.BadZipFile,
            zlib.error,
            subprocess.SubprocessError,
        ) as exc:
            return {
                **record,
                "metadata_ref": receipt.relative_to(root).as_posix(),
                "extraction_error": str(exc),
                "content": "",
                "note": "Complete original saved; text extraction is unavailable. Do not treat this as a source statement.",
            }
        text_path = paths[-1] if paths else None
        if text_path is None:
            return {
                **record,
                "content": "",
                "note": "Complete original saved; no textual source found.",
            }
        record.update(
            status="READY",
            format=kind,
            text_ref=text_path.relative_to(root).as_posix(),
            text_sha256=hashlib.sha256(text_path.read_bytes()).hexdigest(),
        )
        _write_json_atomic(receipt, record)
    text_path = root / record["text_ref"]
    if (
        path_has_symlink_component(root, text_path)
        or not text_path.resolve().is_relative_to(root)
        or hashlib.sha256(text_path.read_bytes()).hexdigest() != record["text_sha256"]
    ):
        raise ValueError("cached source text changed; retain the original version")
    if (
        end_line is None
        and not full_text
        and start_page is None
        and end_page is None
        and start_char is None
        and end_char is None
    ):
        end_line = start_line + 199
    # HTML must use the same content-aware view when fetched and when opened
    # through artifact_ref, including caches created by earlier versions.
    read_path = artifact if record["format"] == "HTML" else text_path
    selection = read_source(
        read_path,
        start_line=start_line,
        end_line=end_line,
        start_page=start_page,
        end_page=end_page,
        start_char=start_char,
        end_char=end_char,
        max_chars=max_chars,
        view=view,
        source_info=record if read_path == artifact else None,
    )
    read_ref = read_path.relative_to(root).as_posix()
    if selection["next_read"] is not None:
        selection["next_read"]["path"] = read_ref
    note = "Text is a reading aid; inspect the original for mathematical layout and symbols."
    if record["format"] == "HTML":
        note += (
            " This is static HTML text, not a browser-rendered page. Ordinary scripts and styles "
            "are omitted from the reading copy, so dynamic content may be absent. "
            "Complete original bytes remain in artifact_ref. Use search_file with view=embedded "
            "to locate page data, then read the matching character range. Do not read the whole web application."
        )
    return {
        **record,
        "url": record["effective_url"],
        "metadata_ref": receipt.relative_to(root).as_posix(),
        "extraction_note": note,
        "read_ref": read_ref,
        **selection,
    }


def acquire_source(workspace, folder, *, origin, local, text_index):
    """Commit downloaded bytes before extraction, so a resume does not refetch."""

    root = workspace.root
    folder.mkdir(parents=True, exist_ok=True)
    receipt_ref = (folder / "source.json").relative_to(root).as_posix()
    receipt_path = root / receipt_ref
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt["status"] == "READY":
            return receipt
    else:
        if local:
            source = root / origin
            artifact = folder / source.name
            shutil.copyfile(source, artifact)
            receipt = {"origin_ref": origin}
        else:
            payload, content_type, effective_url = _fetch_url(origin)
            name = Path(urlparse(effective_url).path).name or "source"
            artifact = folder / name
            artifact.write_bytes(payload)
            receipt = {
                "origin_url": origin,
                "effective_url": effective_url,
                "content_type": content_type,
            }
        receipt.update(
            artifact_ref=artifact.relative_to(root).as_posix(), status="DOWNLOADED"
        )
        workspace.write_json(receipt_ref, receipt)
    artifact = root / receipt["artifact_ref"]
    text_paths, kind = _extract_text(artifact, text_index=text_index)
    receipt.update(
        status="READY",
        format=kind,
        text_refs=[p.relative_to(root).as_posix() for p in text_paths],
    )
    workspace.write_json(receipt_ref, receipt)
    return receipt


def _fetch_url(url):
    request = urllib.request.Request(url, headers={"User-Agent": "pace/2.0"})
    with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
        payload = response.read()
        return payload, response.headers.get_content_type(), response.geturl()


def _extract_text(artifact, *, text_index, encoding="utf-8", base_url=""):
    payload = artifact.read_bytes()
    if text_index:
        artifact.read_text(encoding="utf-8")
        return [artifact], "TEXT"
    if payload[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n\f ").startswith(b"%PDF-"):
        output = artifact.with_name(artifact.name + ".txt")
        output.write_text(
            read_source(artifact, max_chars=None)["content"], encoding="utf-8"
        )
        return [output], "PDF"
    if payload.startswith((b"PK\x03\x04", b"\x1f\x8b")) or tarfile.is_tarfile(artifact):
        destination = artifact.parent / "extracted"
        destination.mkdir(exist_ok=True)
        if payload.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(artifact) as archive:
                for entry in archive.infolist():
                    target = destination / entry.filename
                    if (
                        not target.resolve().is_relative_to(destination.resolve())
                        or "\\" in entry.filename
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    ):
                        raise ValueError("archive member escapes its source directory")
                archive.extractall(destination)
        else:
            try:
                archive = tarfile.open(artifact, mode="r:*")
            except tarfile.ReadError:
                if not payload.startswith(b"\x1f\x8b"):
                    raise
                (destination / "source.tex").write_bytes(gzip.decompress(payload))
            else:
                with archive:
                    archive.extractall(destination, filter="data")
        paths = [
            p
            for p in sorted(destination.rglob("*"))
            if p.is_file()
            and p.suffix.lower() in {".tex", ".txt", ".md", ".bib", ".sty"}
        ]
        index = artifact.parent / "source_index.txt"
        rows = [
            f"[{p.relative_to(destination).as_posix()}:{number}] {line}"
            for p in paths
            for number, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            )
        ]
        index.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return [*paths, index], "ARCHIVE"
    text = payload.decode(encoding)
    if _html_document(text, artifact):
        parser = _HTMLText(base_url)
        parser.feed(text)
        parser.close()
        text = parser.reading_text()
        output = artifact.with_name(artifact.name + ".txt")
        output.write_text(text, encoding="utf-8")
        return [output], "HTML"
    if encoding.lower().replace("_", "-") not in {"utf-8", "utf8"}:
        output = artifact.with_name(artifact.name + ".txt")
        output.write_text(text, encoding="utf-8")
        return [output], "TEXT"
    return [artifact], "TEXT"
