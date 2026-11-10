"""Resume archived runs with their recorded executable version, without migration."""

import hashlib
import json
import os
from pathlib import Path
import sys

from pace.runtime.output.workspace import path_has_symlink_component


def reexec_pinned_runtime(run_dir: Path, arguments: list[str]) -> None:
    """Honor a local run's explicit, hash-bound runtime before touching its state."""
    root = Path(run_dir).expanduser().resolve()
    binding_path = root / "work/state/runtime_binding.json"
    if not binding_path.exists() and not binding_path.is_symlink():
        return
    if path_has_symlink_component(root, binding_path):
        raise ValueError("Runtime binding must be a regular run-local file")
    binding = json.loads(binding_path.read_text())
    if (not isinstance(binding, dict)
            or set(binding) != {"schema_version", "package_ref", "files"}
            or binding["schema_version"] != "pace_runtime_binding/v1"
            or not isinstance(binding["package_ref"], str)
            or not isinstance(binding["files"], dict)):
        raise ValueError("Invalid runtime binding")

    def local_file(base: Path, ref: str) -> Path:
        if not isinstance(ref, str):
            raise ValueError("Runtime references must be relative paths")
        path = Path(ref)
        if (not ref or path.is_absolute() or ".." in path.parts or "\\" in ref
                or path_has_symlink_component(base, base / path)):
            raise ValueError("Runtime references must stay inside their recorded directory")
        return base / path

    package = local_file(root, binding["package_ref"])
    files = binding["files"]
    if package.name != "pace" or not {"__init__.py", "__main__.py"}.issubset(files):
        raise ValueError("Runtime binding must contain a complete pace package")
    if any(p.is_symlink() for p in package.rglob("*")):
        raise ValueError("Pinned runtime must not contain symlinks")
    for ref, digest in files.items():
        file = local_file(package, ref)
        if (not file.is_file() or not isinstance(digest, str)
                or hashlib.sha256(file.read_bytes()).hexdigest() != digest):
            raise ValueError(f"Pinned runtime changed or is missing: {ref}")
    actual = {p.relative_to(package).as_posix() for p in package.rglob("*")
              if p.is_file() and "__pycache__" not in p.relative_to(package).parts}
    if actual != set(files):
        raise ValueError("Pinned runtime contains unrecorded files")
    if package.resolve() == Path(__file__).resolve().parents[1]:
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package.parent) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    os.execvpe(sys.executable, [sys.executable, "-m", "pace", *arguments], env)
