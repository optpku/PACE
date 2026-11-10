"""Check the local model launcher and the production PDF builder offline."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pace.runtime.codex_config import check_codex_isolation
from pace.runtime.output.paper import build_paper


def _probe(
    name: str, executable: str, args: list[str], expected: str,
    *, allowed_exit_codes: tuple[int, ...] = (0,),
) -> dict[str, Any]:
    resolved = shutil.which(executable)
    row = {"name": name, "ok": False, "path": resolved or "", "detail": "not found on PATH"}
    if resolved is None:
        return row
    try:
        result = subprocess.run(
            [resolved, *args], capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL, check=False,
        )
        row["ok"] = (
            result.returncode in allowed_exit_codes
            and re.search(expected, result.stdout + result.stderr, re.I) is not None
        )
        row["detail"] = "available" if row["ok"] else f"local probe failed (exit {result.returncode})"
    except (OSError, subprocess.SubprocessError) as exc:
        row["detail"] = f"cannot run executable ({type(exc).__name__})"
    return row


def check_environment(*, pdf: bool = False) -> dict[str, Any]:
    executable = os.environ.get("PACE_CODEX_CLI_PATH", "codex")
    checks = [
        {"name": "python", "ok": sys.version_info >= (3, 11), "path": sys.executable,
         "detail": platform.python_version()},
        {"name": "posix", "ok": os.name == "posix", "path": "", "detail": platform.system()},
        _probe("ps", "ps", ["-p", str(os.getpid()), "-o", "pid="], str(os.getpid())),
        _probe("codex", executable, ["--version"], "codex"),
        check_codex_isolation(executable),
    ]
    if pdf:
        checks.extend([
            _probe("xelatex", "xelatex", ["--version"], "XeTeX|MiKTeX"),
            _probe("bibtex", "bibtex", ["--version"], "BibTeX"),
            # Xpdf prints its version and exits 99; Poppler exits 0. The
            # production font inspection still requires a successful PDF read.
            _probe("pdffonts", "pdffonts", ["-v"], r"pdffonts\s+version",
                   allowed_exit_codes=(0, 99)),
        ])
        build = {"name": "pdf_compile", "ok": False, "path": "", "detail": "required PDF tools are unavailable"}
        if all(row["ok"] for row in checks[-3:]):
            with TemporaryDirectory(prefix="pace-doctor-") as directory:
                source = Path(directory)
                style = files("pace.resources").joinpath("templates/neurips2026/neurips_2026.sty")
                (source / "neurips_2026.sty").write_bytes(style.read_bytes())
                (source / "main.tex").write_text(
                    r"\documentclass{article}\usepackage[preprint]{neurips_2026}" "\n"
                    r"\begin{document}\section{Check}\label{sec:check}"
                    r"Equation $1+1=2$, Section~\ref{sec:check}, and citation~\citep{check}."
                    r"\bibliographystyle{plainnat}\bibliography{references}\end{document}", encoding="utf-8",
                )
                (source / "references.bib").write_text(
                    "@book{check,author={PACE Contributors},title={Local Build Check},year={2026}}\n",
                    encoding="utf-8",
                )
                result = build_paper(source)
                build.update(ok=result["status"] == "COMPLETE", detail=result["reason"] or "compiled formulas, references and bibliography; checked fonts")
        checks.append(build)
    return {"schema_version": "environment_check/v1", "ok": all(row["ok"] for row in checks),
            "pdf_requested": pdf, "checks": checks,
            "capabilities": execution_capabilities(),
            "note": "No model inference or service-availability check was performed."}


def execution_capabilities() -> dict[str, Any]:
    """Cheap local capability information for mathematical tool use."""
    import importlib.util

    return {
        "python": sys.executable,
        "platform": platform.system(),
        "optional_math_packages": {
            name: importlib.util.find_spec(name) is not None
            for name in ("sympy", "mpmath", "numpy")
        },
        "tools": {
            name: shutil.which(name) or ""
            for name in ("python3", "bash", "xelatex", "bibtex", "pdffonts")
        },
    }
