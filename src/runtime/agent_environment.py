"""Turn-owned Codex homes and fixed instructions for ordinary PACE roles."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pace.runtime.output.workspace import _write_json_atomic, _write_text_atomic, current_turn, work_ref
from pace.runtime.prompts import append_skill_catalog, prompt_root


_ROLES = {
    "GLOBAL_RESEARCH_DIRECTOR": ("director", "global_research_director"),
    "DIRECTOR_LED_FRAMER": ("framer", "research_framing"),
    "SURVEY": ("survey", "director_led_survey"),
    "PLANNER": ("planner", "director_led_planner"),
    "RESEARCH": ("research", "director_led_research"),
    "FINDING_VERIFIER": ("finding_verifier", "finding_verifier"),
    "PROBLEM_ORIENTATION_EXTRACTOR": ("orientation/extractor", "problem_extraction"),
    "PROBLEM_RESEARCH_DECISION": ("orientation/assessment", "research_decision"),
    "WHOLE_PROVER": ("prover/prover", "director_led_prover"),
    "PROOF_CONTRACT_AUDITOR": ("prover/auditor", "proof_contract_auditor"),
    "MATH_VERIFIER_KERNEL": ("prover/kernel", "math_verifier_kernel"),
    "RESEARCH_REPORT_OUTLINE": ("writing/report_outline", "research_report_outline"),
    "RESEARCH_REPORT_WRITER": ("writing/report_writer", "research_report_writer"),
    "THEORY_PAPER_OUTLINE": ("writing/paper_outline", "theory_paper_outline"),
    "THEORY_PAPER_WRITER": ("writing/paper_writer", "theory_paper_writer"),
    "WEB_REPORTER": ("writing/web_reporter", ""),
}

_WEB_REPORTER_INSTRUCTIONS = (
    "You explain already committed mathematical work to its author. All supplied evidence is read-only. "
    "Do not research, prove a missing step, change acceptance or invent progress. "
    "Write user_update.txt in Chinese: 1–3 concrete sentences about the actual attempt, supported outcome and first gap. "
    "Use plain text in user_update.txt: no links, URLs, file paths or navigation instructions. "
    "Distinguish an internal review, a local verified finding and final target acceptance. "
    "A handoff is an unfinished bounded attempt, not mathematical rejection. "
    "When brief.md and findings.md are requested, write brief.md as a self-contained Chinese account "
    "of substantive progress, conditions and the remaining gap, even if no new finding exists. "
    "Write findings.md as a concise thematic navigation of independently verified findings; preserve "
    "each finding's scope and cite original manuscripts. If none exist, say so. Evidence links belong "
    "only in brief.md and findings.md. Use original absolute source paths; never link the copied model workspace."
)
_SPECIALISTS = {"DIRECTOR_LED_FRAMER", "PLANNER", "SURVEY", "RESEARCH", "WHOLE_PROVER"}
_FIELDS = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


def role_home(workdir: Path, role: str, *, environment_root: Path | None = None) -> Path | None:
    """RCI phases and utility calls retain their own invocation environments."""
    definition = _ROLES.get(role.upper())
    if definition is None:
        return None
    root = Path(workdir).resolve()
    environment_root = environment_root or Path(
        os.environ.get("PACE_CODEX_ENV_ROOT", root.parent / ".nl-proof-acp")
    )
    return environment_root / "runs" / root.name / current_turn(root) / definition[0]


def prepare_agent_environments(workdir: Path, *, mode: str | None) -> None:
    """Write role instructions from the committed resource snapshot before calls."""
    root = Path(workdir).resolve()
    resources = prompt_root(root)
    guides = tuple(f"guides/{path.name}" for path in sorted((resources / "guides").glob("*.md")))
    for role, (_, template) in _ROLES.items():
        home = role_home(root, role)
        if role == "WEB_REPORTER":
            body = _WEB_REPORTER_INSTRUCTIONS
        else:
            if role == "WHOLE_PROVER" and mode == "proof":
                template = "proof_prover"
            refs = [f"roles/{template}.md"]
            if role in _SPECIALISTS:
                if mode == "proof" and role == "WHOLE_PROVER":
                    refs.extend(guides)
                elif not (mode == "proof" and role == "SURVEY"):
                    refs.extend(("shared/transport.md", *guides,
                                 "modes/RESEARCH_FRAMING.md" if role == "DIRECTOR_LED_FRAMER" else "modes/SPECIALIST.md",
                                 "output/files.md", "output/findings.md", "output/memory.md"))
                if role == "DIRECTOR_LED_FRAMER":
                    refs.extend(("kernel/literal_target.md", "contracts/research.md", "kernel/artifact_writing.md"))
            if role in {"PROOF_CONTRACT_AUDITOR", "MATH_VERIFIER_KERNEL"}:
                refs.append("output/structured.md")
            if role in {"FINDING_VERIFIER", "MATH_VERIFIER_KERNEL"}:
                refs.append("output/finding_publication.md")
            if role == "MATH_VERIFIER_KERNEL":
                refs.extend(("output/findings.md", "output/memory.md"))
            fragments = []
            for ref in refs:
                text = (resources / ref).read_text(encoding="utf-8")
                # Existing placeholders become references, never stale task values.
                text = _FIELDS.sub(
                    lambda match: work_ref(root) if match[1] == "turn_dir" else f"TASK.{match[1]}",
                    text,
                )
                fragments.append(f"<!-- Source: {ref} -->\n{text.strip()}")
            body = "\n\n".join(fragments)
        if role == "GLOBAL_RESEARCH_DIRECTOR":
            body = append_skill_catalog(body, workdir=root)
        instructions = (
            f"# PACE role: {role}\n\n"
            f"Problem directory: `{root}`\nCurrent turn: `{work_ref(root)}`\n\n"
            "These are fixed role instructions. TASK.field references mean the value "
            "provided under that exact field name in the current task prompt, not a "
            "literal path or mathematical premise. Current inputs, target, feedback, "
            "outputs and budgets are supplied separately on every call.\n\n" + body + "\n"
        )
        path = home / "AGENTS.md"
        if not path.is_file() or path.read_text(encoding="utf-8") != instructions:
            _write_text_atomic(path, instructions)


def director_thread(home: Path) -> str:
    path = home / "session.json"
    return json.loads(path.read_text(encoding="utf-8"))["thread_id"] if path.is_file() else ""


def record_native_session(*, home: Path, role: str, stdout_path: Path, record_path: Path) -> None:
    """Persist the native ID even when an invocation times out or is interrupted."""
    for line in stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "thread.started":
            continue
        thread_id = event["thread_id"]
        if role.upper() == "GLOBAL_RESEARCH_DIRECTOR":
            previous = director_thread(home)
            if previous and previous != thread_id:
                raise ValueError("Director resumed a different native session")
            _write_json_atomic(home / "session.json", {"thread_id": thread_id})
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.update(thread_id=thread_id)
        _write_json_atomic(record_path, record)
        return
