"""Preserve actual input bytes across a model call, including inline writing."""

import difflib
import json
import threading
from pathlib import Path

_ORIGINALS_MUTEX = threading.Lock()


class InputModifiedError(ValueError):
    pass


class protected_inputs:
    def __init__(self, root, refs, invocation):
        self.root = Path(root)
        fixed = [self.root / str(ref) for ref in refs if str(ref)]
        from pace.runtime.output.workspace import work_ref

        critical = list(
            self.root / work_ref(root, f"input/{name}")
            for name in ("problem.md", "target_contract.json")
        )
        # These immutable originals may be reached through a catalogue link
        # rather than explicitly named in this call's suggested input list.
        from pace.runtime.output.results import finding_entries, finding_publications

        for row in finding_entries(self.root):
            fixed.append(self.root / row["ref"])
        for row in finding_publications(self.root):
            critical.extend(self.root / row[key] for key in ("source_ref", "verification_ref"))
        terminal = self.root / work_ref(root, "final_report.json")
        if terminal.is_file():
            report = json.loads(terminal.read_text(encoding="utf-8"))
            evidence = report.get("artifact_refs", {})
            for name in ("proof", "candidate", "proof_manifest", "verification_binding",
                         "verification", "auditor", "kernel", "source_authority"):
                if evidence.get(name):
                    critical.append(self.root / evidence[name])
            if evidence.get("verification"):
                directory = (self.root / evidence["verification"]).parent
                critical.extend(directory / name for name in ("verification_inputs.json", "final_result.json"))
        self.originals = {
            path.resolve(): path.read_bytes() for path in fixed if path.is_file()
        }
        # Concurrent calls share one immutable original for Host-owned targets
        # and accepted evidence. A later call must not bless an earlier edit.
        directory = self.root / work_ref(root, "state/protected_originals")
        directory.mkdir(parents=True, exist_ok=True)
        with _ORIGINALS_MUTEX:
            manifest = directory / "manifest.json"
            index = json.loads(manifest.read_text()) if manifest.is_file() else {}
            for path in dict.fromkeys(critical):
                path = path.absolute()
                key = str(path)
                if key not in index:
                    if not path.is_file():
                        continue
                    name = f"{len(index):04d}.original"
                    (directory / name).write_bytes(path.read_bytes())
                    index[key] = name
                self.originals[path] = (directory / index[key]).read_bytes()
            from pace.runtime.output.workspace import _write_json_atomic

            _write_json_atomic(manifest, index)
        self.invocation = invocation
        self.snapshot = Path(invocation) / "protected_inputs"
        self.snapshot.mkdir(parents=True, exist_ok=True)
        self.manifest = {"status": "PREPARED", "entries": []}
        for index, (path, original) in enumerate(self.originals.items()):
            name = f"{index:04d}.original"
            (self.snapshot / name).write_bytes(original)
            self.manifest["entries"].append({"path": str(path), "snapshot": name})
        self._save()

    def _save(self):
        from pace.runtime.output.workspace import _write_json_atomic

        _write_json_atomic(self.snapshot / "manifest.json", self.manifest)

    def arm(self):
        self.manifest["status"] = "OPEN"
        self._save()

    def check(self, attempt):
        changed = []
        for path, original in self.originals.items():
            if not path.is_symlink() and path.is_file() and path.read_bytes() == original:
                continue
            saved = Path(attempt) / "modified_inputs" / str(len(changed))
            saved.parent.mkdir(parents=True, exist_ok=True)
            modified = path.read_bytes() if path.is_file() else b""
            if path.is_symlink():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
            saved.write_bytes(modified)
            saved.with_suffix(".diff").write_text("".join(difflib.unified_diff(
                original.decode("utf-8", errors="backslashreplace").splitlines(keepends=True),
                modified.decode("utf-8", errors="backslashreplace").splitlines(keepends=True),
                fromfile=str(path), tofile="rejected modified input",
            )), encoding="utf-8")
            saved.with_suffix(".json").write_text(json.dumps({
                "original_path": str(path), "invocation_id": str(self.invocation),
                "action": "rejected and restored original bytes",
            }, ensure_ascii=False), encoding="utf-8")
            changed.append(str(path))
        self.manifest.update(status="CLOSED", restored_paths=changed)
        self._save()
        if changed:
            raise InputModifiedError(
                "Input files were modified and restored: " + ", ".join(changed)
            )


def recover_open_input_snapshots(root: Path) -> None:
    """Restore only unfinished calls found through the existing event index."""
    from pace.runtime.output.events import read_events, append_event

    seen = set()
    for event in reversed(read_events(root)):
        ref = event.get("call_ref", "")
        if event["event_kind"] != "SEMANTIC_CALL_STARTED" or not ref or ref in seen:
            continue
        seen.add(ref)
        directory = (Path(root) / ref).parent
        manifest = directory / "protected_inputs/manifest.json"
        if not manifest.is_file():
            continue
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if value["status"] != "OPEN":
            continue
        protector = object.__new__(protected_inputs)
        protector.root, protector.invocation = Path(root), directory
        protector.snapshot, protector.manifest = manifest.parent, value
        protector.originals = {
            Path(row["path"]): (manifest.parent / row["snapshot"]).read_bytes()
            for row in value["entries"]
        }
        try:
            protector.check(directory / "input_recovery")
        except InputModifiedError:
            append_event(root, "INPUTS_RESTORED", call_ref=ref,
                         paths=protector.manifest["restored_paths"],
                         reason="unfinished call's original bytes restored before checkpoint recovery")
