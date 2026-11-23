"""Build delivered LaTeX in isolation without judging its mathematics."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from pace.artifacts.workspace import _write_json_atomic, _write_text_atomic
from pace.runtime.math_format import require_math_format


def build_paper(directory: Path) -> dict[str, str]:
    directory = Path(directory)
    pdf = directory / "main.pdf"
    pdf.unlink(missing_ok=True)
    status, reason = ("COMPLETE", "")
    logs: list[str] = []

    def run(command: list[str], build: Path, *, timeout: int) -> str:
        if shutil.which(command[0]) is None:
            raise FileNotFoundError(f"{command[0]} is not installed")
        result = subprocess.run(
            command,
            cwd=build,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        logs.append("$ " + " ".join(command) + "\n" + result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"{command[0]} exited with {result.returncode}")
        return result.stdout

    try:
        with TemporaryDirectory(
            prefix=".paper-build-", dir=directory.parent
        ) as temporary:
            build = Path(temporary)
            shutil.copytree(
                directory,
                build,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "main.pdf",
                    "build.json",
                    "build.log",
                    "latex-source.zip",
                    "*.aux",
                    "*.log",
                    "*.out",
                    "*.toc",
                    "*.blg",
                    "*.fls",
                    "*.fdb_latexmk",
                    "*.synctex.gz",
                ),
            )
            for tex in build.rglob("*.tex"):
                require_math_format(tex.read_text(encoding="utf-8"), markdown=False)
            run(
                [
                    "latexmk",
                    "-norc",
                    "-xelatex",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-no-shell-escape",
                    "main.tex",
                ],
                build,
                timeout=720,
            )
            transcript = (build / "main.log").read_text(
                encoding="utf-8", errors="replace"
            )
            if re.search(
                "There were undefined (?:references|citations)\\.", transcript
            ):
                raise RuntimeError("Unresolved references or citations remain")
            built_pdf = build / "main.pdf"
            if not built_pdf.is_file() or not built_pdf.stat().st_size:
                raise RuntimeError("latexmk did not produce a PDF")
            fonts = run(["pdffonts", "main.pdf"], build, timeout=180)
            if re.search("\\bType\\s+3\\b", fonts):
                raise RuntimeError(
                    "PDF contains Type 3 fonts; use installed vector fonts"
                )
            shutil.copyfile(built_pdf, pdf)
    except FileNotFoundError as exc:
        status, reason = ("UNAVAILABLE", str(exc))
    except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError) as exc:
        status, reason = ("FAILED", str(exc))
    if status != "COMPLETE":
        pdf.unlink(missing_ok=True)
    _write_text_atomic(
        directory / "build.log", "\n\n".join(logs) + "\n" + reason + "\n"
    )
    result = {"status": status, "reason": reason}
    _write_json_atomic(directory / "build.json", result)
    return result
