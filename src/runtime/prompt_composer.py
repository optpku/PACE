"""Deterministic assembly of fixed mathematical role instructions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pace.runtime.output.workspace import _write_json_atomic
from pace.runtime.prompt_policy import PromptAssembly


class PromptComposerError(ValueError):
    """A missing input or invalid assembly at the Host prompt boundary."""

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
    workdir: Path,
    assembly: PromptAssembly,
    input_refs: Iterable[str],
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
            "verification_receipt_ref": row["verification_ref"],
            "receipt_reuse_authorized": receipt.get("verdict", receipt).get("reuse_authorized", False),
            "current_target_contract_id": target_contract_id,
            "target_relationship": "To be established explicitly in this candidate; not inferred by the Host.",
        })
        table.append(f"| {row['finding_id']} | `{row['source_ref']}` lines 1–{len(body.splitlines())} | `{row['verification_ref']}` |")
    if not entries:
        table.append("| None | No checked finding dependency is available. | — |")
    ref = str(Path(output_ref).with_name("dependency_manifest.json"))
    _write_json_atomic(workdir / ref, {"target_contract_id": target_contract_id, "entries": entries})
    table.append(f"Source and receipt index: `{ref}`. Read each dependency's original proof before using it; bodies are not duplicated in this index.")
    return ref, "\n".join(table)


def _format_budget(value: Any) -> str:
    if value is None:
        return "(none)"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "PromptComposerError",
    "PromptComposerResult",
    "compose_prompt",
]
