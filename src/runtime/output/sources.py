"""Acquire Survey files and persist resumable, non-authoritative source context."""
from __future__ import annotations

import gzip
import html
import json
import re
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlparse

from pace.runtime.output.workspace import RunWorkspace


def process_survey_sources(*, workdir: Path, report_ref: str, allow_network: bool) -> tuple[str, ...]:
    """Process explicit file/URL requests; mathematical source claims stay opaque."""

    root = Path(workdir).resolve()
    workspace = RunWorkspace(root=root, problem_id="survey")
    folder = (root / report_ref).parent
    sources = folder / "sources"
    manifest = folder / "source_manifest.json"
    refs = [p.relative_to(root).as_posix() for p in sorted(sources.rglob("*")) if p.is_file()]
    if not manifest.is_file():
        return tuple(refs)
    processing_ref = (folder / "source_processing.json").relative_to(root).as_posix()
    processing = {"authority": "SEARCH_CONTEXT_ONLY", "items": [], "diagnostics": []}
    refs.append(manifest.relative_to(root).as_posix())
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("source manifest must contain an items list")
        previous = root / processing_ref
        if previous.is_file():
            saved = json.loads(previous.read_text(encoding="utf-8"))
            if (saved.get("source_manifest") == payload
                    and saved.get("allow_network") is allow_network and saved.get("completed")):
                return tuple(saved["published_refs"])
        processing.update(source_manifest=payload, allow_network=allow_network)
        for number, item in enumerate(payload["items"], 1):
            record = {"item": item, "files": [], "diagnostics": []}
            processing["items"].append(record)
            try:
                if not isinstance(item, dict):
                    raise ValueError("source item must be an object")
                local_ref = ""
                for key in ("text_index_ref", "artifact_ref"):
                    ref = item.get(key)
                    if not ref:
                        continue
                    path = (root / ref).resolve()
                    path.relative_to(root)
                    if path.is_file():
                        local_ref = path.relative_to(root).as_posix()
                        refs.append(local_ref)
                        break
                    record["diagnostics"].append({"reason_code": "source_file_missing", "ref": ref})
                urls = _source_urls(item)
                if local_ref:
                    origins = [(local_ref, True)]
                elif allow_network and urls:
                    origins = [(url, False) for url in urls]
                else:
                    record["diagnostics"].append({"reason_code": (
                        "source_retrieval_disabled" if not allow_network else "source_location_missing"
                    )})
                    continue
                for index, (origin, local) in enumerate(origins, 1):
                    try:
                        file = _acquire_file(
                            workspace, sources / f"item_{number:04d}" / f"file_{index:02d}",
                            origin=origin, local=local,
                            text_index=bool(local_ref and item.get("text_index_ref") == local_ref),
                        )
                        record["files"].append(file)
                    except (OSError, ValueError, EOFError, tarfile.TarError,
                            zipfile.BadZipFile, zlib.error, subprocess.SubprocessError) as exc:
                        record["diagnostics"].append({
                            "reason_code": "source_processing_failed", "origin": origin, "message": str(exc),
                        })
                record["literal_locations"] = [
                    excerpt for file in record["files"]
                    for excerpt in _literal_locations(root, file, str(item.get("source_locator") or ""))
                ]
                if item.get("source_locator") and not record["literal_locations"]:
                    record["diagnostics"].append({
                        "reason_code": "source_locator_unresolved", "source_locator": item["source_locator"],
                    })
            except (OSError, ValueError, TypeError) as exc:
                record["diagnostics"].append({"reason_code": "source_processing_failed", "message": str(exc)})
    except (OSError, ValueError) as exc:
        processing["diagnostics"].append({"reason_code": "source_manifest_invalid", "message": str(exc)})
    refs.extend((processing_ref, *(p.relative_to(root).as_posix()
                 for p in sorted(sources.rglob("*")) if p.is_file())))
    processing.update(completed=True, published_refs=list(dict.fromkeys(refs)))
    workspace.write_json(processing_ref, processing)
    return tuple(processing["published_refs"])


def _source_urls(item):
    canonical = str(item.get("canonical_url") or "")
    urls = [item.get("primary_artifact_url")]
    arxiv = re.search(r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf|e-print|src)/([^?#]+)", canonical)
    if arxiv:
        paper_id = arxiv[1].rstrip("/").removesuffix(".pdf")
        urls.extend((f"https://arxiv.org/e-print/{paper_id}", f"https://arxiv.org/pdf/{paper_id}"))
    urls.append(canonical)
    if arxiv:
        urls.append(f"https://arxiv.org/abs/{paper_id}")
    return list(dict.fromkeys(filter(None, urls)))


def _acquire_file(workspace, folder, *, origin, local, text_index):
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
            receipt = {"origin_url": origin, "effective_url": effective_url, "content_type": content_type}
        receipt.update(artifact_ref=artifact.relative_to(root).as_posix(), status="DOWNLOADED")
        workspace.write_json(receipt_ref, receipt)
    artifact = root / receipt["artifact_ref"]
    text_paths, kind = _extract_text(artifact, text_index=text_index)
    receipt.update(status="READY", format=kind,
                   text_refs=[p.relative_to(root).as_posix() for p in text_paths])
    workspace.write_json(receipt_ref, receipt)
    return receipt


def _fetch_url(url):
    request = urllib.request.Request(url, headers={"User-Agent": "pace/2.0"})
    with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
        payload = response.read(48_000_001)
        if len(payload) > 48_000_000:
            raise ValueError("source exceeds the 48 MB download limit")
        return payload, response.headers.get_content_type(), response.geturl()


def _extract_text(artifact, *, text_index):
    payload = artifact.read_bytes()
    if text_index:
        artifact.read_text(encoding="utf-8")
        return [artifact], "TEXT"
    if payload[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n\f ").startswith(b"%PDF-"):
        output = artifact.with_name(artifact.name + ".pdftotext.txt")
        subprocess.run(["pdftotext", "-layout", str(artifact), str(output)],
                       check=True, capture_output=True, timeout=45)
        return [output], "PDF"
    if payload.startswith((b"PK\x03\x04", b"\x1f\x8b")) or tarfile.is_tarfile(artifact):
        destination = artifact.parent / "extracted"
        destination.mkdir(exist_ok=True)
        if payload.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(artifact) as archive:
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
        paths = [p for p in sorted(destination.rglob("*"))
                 if p.is_file() and p.suffix.lower() in {".tex", ".txt", ".md", ".bib", ".sty"}]
        index = artifact.parent / "source_index.txt"
        rows = [f"[{p.relative_to(destination).as_posix()}:{number}] {line}"
                for p in paths for number, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)]
        index.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return [*paths, index], "ARCHIVE"
    text = payload.decode("utf-8")
    if re.search(r"<(?:!doctype\s+html|html|head|body)\b", text, re.I):
        text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.I | re.S)
        text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
        output = artifact.with_name(artifact.name + ".txt")
        output.write_text(text, encoding="utf-8")
        return [output], "HTML"
    return [artifact], "TEXT"


def _literal_locations(root, file, locator):
    """Resolve physical file/line/page spans; named theorems remain agent work."""

    pages = re.search(r"\bpp?\.?\s*(\d+)(?:\s*[-–]\s*(\d+))?", locator, re.I)
    lines = re.search(r"\blines?\s*(\d+)(?:\s*[-–]\s*(\d+))?", locator, re.I)
    named = re.search(r"([\w./-]+\.(?:tex|txt|md|pdf|html?))\b", locator, re.I)
    for ref in file["text_refs"]:
        path = root / ref
        if named:
            name = named.group(1)
            if not (ref.endswith('/' + name) or Path(file["artifact_ref"]).name == name):
                continue
        text = path.read_text(encoding="utf-8")
        if pages and (file["format"] == "PDF" or "\f" in text):
            start, end = int(pages[1]), int(pages[2] or pages[1])
            chunks = text.split("\f")
            if 1 <= start <= end <= len(chunks):
                yield {"text_ref": ref, "pages": [start, end], "text": "\f".join(chunks[start-1:end])}
        elif lines and not pages:
            start, end = int(lines[1]), int(lines[2] or lines[1])
            chunks = text.splitlines()
            if 1 <= start <= end <= len(chunks):
                yield {"text_ref": ref, "lines": [start, end], "text": "\n".join(chunks[start-1:end])}
