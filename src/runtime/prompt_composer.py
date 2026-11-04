"""Deterministic assembly of fixed mathematical role instructions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pace.runtime.output.workspace import (
    _write_json_atomic,
    _write_text_atomic,
)
from pace.runtime.prompt_policy import PromptAssembly


class PromptComposerError(ValueError):
    """A Composer transport or output failure owned by the Host interface."""

    failure_owner = "HOST"
    reason_code = "prompt_composer_interface_failed"

    def __init__(self, message: str, *, diagnostics: Iterable[dict[str, Any]] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


@dataclass(frozen=True)
class PromptComposerResult:
    final_prompt: str
    call_id: str


def compose_prompt(
    *,
    llm: Any,
    workdir: Path,
    assembly: PromptAssembly,
    input_refs: Iterable[str],
    call_name: str,
    semantic_call_fn: Any | None = None,
    round_index: int = 0,
) -> PromptComposerResult:
    """Render a fixed role task without a model call."""
    from pace.runtime.prompts import prompt_root

    root = Path(workdir).resolve()
    resources = prompt_root(root).resolve()
    guidance, inputs = [], []
    for ref in dict.fromkeys(
        (*assembly.required_refs, *input_refs, *assembly.candidate_refs)
    ):
        path = (root / ref).resolve()
        if not path.is_file():
            if ref in assembly.required_refs:
                raise PromptComposerError(f"required task input is missing: {ref}")
            continue
        if path.is_relative_to(resources):
            guidance.append(path.read_text(encoding="utf-8").strip())
        else:
            inputs.append(ref)
    prompt = "\n\n".join(
        (
            *dict.fromkeys(guidance),
            f"## Target\n{assembly.target_contract_id}",
            "## Assigned task\n" + assembly.prompt_seed,
            "## Files to read\n" + "\n".join(f"- `{ref}`" for ref in inputs),
            f"## Deliverable\nWrite `{assembly.output_artifact_ref}`.",
            "## Budget\n" + _format_budget(assembly.budget),
        )
    )
    return PromptComposerResult(final_prompt=prompt, call_id="")


def write_dependency_manifest(*, workdir: Path, output_ref: str,
                              target_contract_id: str) -> tuple[str, str]:
    """Index exact finding sources and receipts without inventing proof authority."""
    from pace.runtime.output.results import finding_entries

    entries, table = [], [
        "## Checked dependency sources",
        "Read the exact statements and assumptions at the source locations below. "
        "These are candidate dependencies, not automatic premises. The Prover must state "
        "each dependency's relation to the current target. Reconstruct every load-bearing "
        "derivation unless the actual verifier explicitly authorizes inheritance for this "
        "target and exact version; a finding PASS alone does not grant that authority.",
        "| Finding | Exact statement, assumptions and proof | Verifier receipt |",
        "|---|---|---|",
    ]
    for row in finding_entries(workdir):
        source = workdir / row["source_ref"]
        body = source.read_text(encoding="utf-8")
        title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), row["finding_id"])
        receipt = json.loads((workdir / row["verification_ref"]).read_text(encoding="utf-8"))
        entries.append({
            **row, "statement_and_assumptions_ref": row["source_ref"],
            "source_lines": [1, len(body.splitlines())], "source_summary_verbatim": title,
            "source_text": body, "verification_receipt": receipt,
            "current_target_contract_id": target_contract_id,
            "target_relationship": "To be established explicitly in this candidate; not inferred by the Host.",
        })
        table.append(f"| {row['finding_id']} | `{row['source_ref']}` lines 1–{len(body.splitlines())} | `{row['verification_ref']}` |")
    if not entries:
        table.append("| None | No checked finding dependency is available. | — |")
    ref = str(Path(output_ref).with_name("dependency_manifest.json"))
    _write_json_atomic(workdir / ref, {"target_contract_id": target_contract_id, "entries": entries})
    table.append(f"Exact source and receipt snapshot: `{ref}`. Inspect original proofs as needed.")
    return ref, "\n".join(table)


def record_composer_manifest(path: Path, result: PromptComposerResult) -> None:
    """Add Composer provenance to an existing assembly manifest."""

    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromptComposerError(
            "Prompt assembly manifest is unavailable",
            diagnostics=({"stage": "prompt_composer", "reason": str(exc)},),
        ) from exc
    if not isinstance(payload, dict):
        raise PromptComposerError("Prompt assembly manifest is not an object")
    payload.update(
        {
            "composer_call_id": result.call_id,
            "final_prompt_ref": str(target.with_name("final_prompt.md")),
            "assembly_source": "template",
        }
    )
    prompt_ref = target.with_name("final_prompt.md")
    try:
        _write_text_atomic(prompt_ref, result.final_prompt)
        _write_json_atomic(target, payload)
    except (OSError, UnicodeError) as exc:
        raise PromptComposerError(
            "Prompt assembly manifest could not be written",
            diagnostics=(
                {
                    "stage": "prompt_composer",
                    "reason_code": "manifest_write_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            ),
        ) from exc


def _format_budget(value: Any) -> str:
    if value is None:
        return "(none)"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _input_guidance(paths: Iterable[str], assembly: PromptAssembly, root: Path) -> str:
    """Label suggested reading paths; the role can discover further context."""

    groups = {
        label: {(root / ref).resolve() for ref in refs}
        for label, refs in (
            ("required", assembly.required_refs),
            ("contract guide", assembly.contract_guide_refs),
            ("artifact guide", assembly.artifact_guide_refs),
        )
    }
    lines = []
    for ref in paths:
        labels = [label for label, refs in groups.items() if (root / ref).resolve() in refs]
        lines.append(f"- `{ref}` — {', '.join(labels) or 'on-demand context'}")
    return "\n".join(lines) or "- (none)"


__all__ = [
    "PromptComposerError",
    "PromptComposerResult",
    "compose_prompt",
    "record_composer_manifest",
]
