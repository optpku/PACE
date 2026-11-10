"""Persist agent-authored notes and index their original evidence references."""

from __future__ import annotations

import json
import logging
import os
import hashlib
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from pace.runtime.context_state import empty_search_progress
from .results import FINDINGS_ARCHIVE_REF, FINDINGS_INDEX_REF, relocate_links, finding_entries, finding_publications
from .workspace import RunWorkspace, work_ref, path_has_symlink_component, is_call_path

logger = logging.getLogger(__name__)

ROUTE_RELATIONS_POLICY = (
    "The relation vocabulary is open; this replaces any older fixed list of relation types. "
    "Each relation is a descriptive lower_snake_case label (1-64 characters), "
    "for example supports, depends_on, reopens or refutes. Other labels such as "
    "supports_search are allowed when their explanation states the exact scoped use. "
    "These labels are navigation only: they never authorize proof reuse, close an "
    "obligation or change a verification verdict. Do not relabel a refutation as support. "
)


def note_ref(result_ref: str, *, verification: bool = False) -> str:
    path = Path(result_ref)
    suffix = "-verification" if verification else ""
    return f"memory/notes/{path.parts[1]}-{path.parent.name}{suffix}.md"


def initialize_memory(workspace: RunWorkspace, *, context_manifest: dict) -> None:
    """Retain scoped evidence across turns and bind the current progress view."""

    path = workspace.path("memory/verified_progress.json")
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    progress = empty_search_progress(
        target_contract_id=context_manifest["target_contract_id"],
        target_contract_ref=context_manifest["target_contract_ref"],
    )
    for key in ("verified_relations", "rejected_relations"):
        progress[key] = previous.get(key, [])
    workspace.write_json("memory/active_problem_dossier.json", context_manifest)
    workspace.write_json("memory/verified_progress.json", progress)
    workspace.path("memory/notes").mkdir(exist_ok=True)
    refresh_memory_index(workspace.root)


def commit_memory_notes(
    root: Path, *, result_refs: Iterable[str],
    verification_values: Iterable[Mapping] = (),
) -> tuple[str, ...]:
    """Only the parent publishes committed task or bound Kernel drafts once."""

    workspace = RunWorkspace(root=root, problem_id=root.name)
    published = []
    for result_ref in dict.fromkeys(result_refs):
        if Path(result_ref).name != "agent_result.json":
            continue
        result = json.loads((root / result_ref).read_text(encoding="utf-8"))
        if result["status"] != "SUCCEEDED":
            continue
        directory = Path(result_ref).parent
        target_ref = f"work/{directory.parts[1]}/input/target_contract.json"
        ref = _publish_note(workspace, source_ref=f"{directory}/memory_note.md",
                            destination_ref=note_ref(result_ref),
                            evidence_refs=(target_ref, result["deliverable_ref"], result_ref))
        if ref:
            published.append(ref)
    for value in verification_values:
        progress_ref = value.get("verified_progress_ref")
        if not progress_ref:
            continue
        local = json.loads((root / progress_ref).read_text(encoding="utf-8"))
        directory = Path(progress_ref).parent
        result_ref = f"{directory.parent}/agent_result.json"
        ref = _publish_note(workspace, source_ref=f"{directory}/memory_note.md",
                            destination_ref=note_ref(result_ref, verification=True),
                            evidence_refs=(local["target_contract_ref"], local["last_proof_ref"],
                                           local["last_verification_ref"], value["verification_binding_ref"]))
        if ref:
            published.append(ref)
    try:
        refresh_memory_index(root)
    except OSError:
        logger.warning("Memory notes committed; navigation refresh failed", exc_info=True)
    return tuple(dict.fromkeys(published))


def _publish_note(workspace: RunWorkspace, *, source_ref: str, destination_ref: str,
                  evidence_refs: tuple[str, ...]) -> str:
    source = workspace.path(source_ref)
    if not source.is_file():
        return ""
    destination = workspace.path(destination_ref)
    if destination.is_file():
        return destination_ref
    try:
        body = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("Cannot read optional memory draft: %s", source_ref, exc_info=True)
        return ""
    if not body.strip():
        return ""
    body = relocate_links(body, source, destination)
    lines = [body.rstrip(), "", "---", "", "用途：SEARCH_CONTEXT_ONLY。范围及依据由以下原始文件保留："]
    for ref in dict.fromkeys(ref for ref in (source_ref, *evidence_refs) if ref):
        link = Path(os.path.relpath(workspace.path(ref), destination.parent)).as_posix()
        lines.append(f"- [{ref}]({link})")
    try:
        workspace.write_text_atomic(destination_ref, "\n".join(lines) + "\n")
    except OSError:
        logger.warning("Cannot publish optional memory draft: %s", source_ref, exc_info=True)
        return ""
    return destination_ref


def refresh_memory_index(root: Path) -> None:
    """Generate navigation without summarizing or judging mathematics."""

    workspace = RunWorkspace(root=root, problem_id=root.name)
    refresh_research_index(root)
    lines = ["# 研究记忆", "", "记忆条目用于寻找研究上下文。复用时读取原始目标、假设、论证与核验记录；本轮验收以当前 literal target 为准。", "",
             "## 当前上下文", ""]
    for label, ref in (("当前问题", work_ref(root, "input/problem.md")),
                       ("当前目标", work_ref(root, "input/target_contract.json")),
                       ("输入索引", "memory/active_problem_dossier.json"),
                       ("成果、应用关系与当前任务索引", "memory/research_index.json"),
                       ("整体验证的历史关系（不等于当前全部成果或开放任务）", "memory/verified_progress.json")):
        link = Path(os.path.relpath(root / ref, root / "memory")).as_posix()
        lines.append(f"- [{label}]({link})")
    lines.extend(["", "既往核验关系保留原始目标引用；跨轮使用需要重新检查范围与应用条件。"])
    latest_plan = ""
    for path in sorted(root.glob("work/turn*/round*/agent_result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["agent_id"] == "planner" and result["status"] == "SUCCEEDED":
            latest_plan = result["deliverable_ref"]
    if latest_plan:
        link = Path(os.path.relpath(root / latest_plan, root / "memory")).as_posix()
        lines.extend(["", f"最近 Planner 交付物（适用范围见原稿）：[{latest_plan}]({link})"])
    lines.extend(["", "## 记忆条目", ""])
    notes = sorted((root / "memory/notes").glob("*.md"))
    lines.extend(f"- [{p.stem}](notes/{p.name})" for p in notes)
    if not notes:
        lines.append("尚无已提交的研究记忆。")
    lines.extend(["", "## 成果入口", ""])
    for label, ref in (("当前成果总览", FINDINGS_INDEX_REF), ("核验原稿档案", FINDINGS_ARCHIVE_REF)):
        if (root / ref).is_file():
            link = Path(os.path.relpath(root / ref, root / "memory")).as_posix()
            lines.append(f"- [{label}]({link})")
    workspace.write_text_atomic("memory/README.md", "\n".join(lines) + "\n")


def validate_route_relations(root: Path, ref: str) -> dict:
    """Validate navigation references, never decide mathematical applicability."""
    def check(reference):
        if not isinstance(reference, str) or not reference:
            raise ValueError("empty relation reference")
        file = Path(reference.split("#", 1)[0])
        if "\\" in reference or file.is_absolute() or ".." in file.parts or is_call_path(file) or path_has_symlink_component(root, root / file) or not (root / file).is_file():
            raise ValueError(f"invalid relation reference: {reference}")

    check(ref)
    value = json.loads((root / ref.split("#", 1)[0]).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("relations"), list) or not isinstance(value.get("open_obligations"), list):
        raise ValueError("route_relations requires relations and open_obligations lists")
    for reference in value["open_obligations"]:
        check(reference)
    for index, row in enumerate(value["relations"]):
        if not isinstance(row, dict):
            raise ValueError(f"relations[{index}] must be an object")
        label = row.get("relation")
        if (not isinstance(label, str) or not 1 <= len(label) <= 64
                or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", label) is None):
            raise ValueError(f"relations[{index}].relation must be a lower_snake_case label of 1-64 characters; got {label!r}")
        check(row.get("from_ref")); check(row.get("to_ref"))
        if not isinstance(row.get("explanation"), str) or not row["explanation"].strip():
            raise ValueError(f"relations[{index}] needs a concise mathematical explanation")
        if row.get("review_ref") not in (None, ""):
            check(row["review_ref"])
    return value


def refresh_research_index(root: Path) -> None:
    """Join existing ledgers with versioned, model-declared uses; no proof inference."""
    workspace = RunWorkspace(root=root, problem_id=root.name)
    records = []
    research_records = []
    for file in sorted(root.glob("work/turn*/round*/agent_result.json")):
        result = json.loads(file.read_text())
        if result.get("status") != "SUCCEEDED":
            continue
        research_records.append({
            "role": result["agent_id"], "result_ref": file.relative_to(root).as_posix(),
            "artifact_ref": result["deliverable_ref"],
            "authority": "SEARCH_CONTEXT_ONLY",
            "question_versions_refs": [ref for ref in result.get("published_refs", ())
                                       if Path(ref).name == "question_versions.json"],
        })
    for row in finding_entries(root):
        records.append({
            "id": row["finding_id"], "kind": "verified_finding",
            "statement_scope_and_proof_ref": row["source_ref"],
            "verification_ref": row["verification_ref"],
            "version": hashlib.sha256((root / row["source_ref"]).read_bytes()).hexdigest(),
            "application_status": "CHECK_CURRENT_USE",
        })
    route_path = root / work_ref(root, "state/current_route.json")
    route = json.loads(route_path.read_text()) if route_path.is_file() else {}
    relations_ref = route.get("relations_ref", "")
    relations = validate_route_relations(root, relations_ref) if relations_ref else {}
    # A Planner suggestion is not a live queue item. Only explicit Director
    # selection binds a mathematical worker to it; delivery is not verification.
    task_ref = route.get("next_task_ref", "")
    task_uses = []
    if task_ref:
        for file in sorted((root / work_ref(root)).glob("*/task.json")):
            assignment = json.loads(file.read_text())
            if assignment.get("agent_id") not in {"research", "prover", "rci"}:
                continue
            decision_path = root / assignment.get("director_decision_ref", "")
            if not decision_path.is_file():
                continue
            decision = json.loads(decision_path.read_text())
            if task_ref not in decision.get("input_refs", ()):
                continue
            result_path = file.parent / "agent_result.json"
            result = json.loads(result_path.read_text()) if result_path.is_file() else {}
            task_uses.append({"assignment_ref": file.relative_to(root).as_posix(),
                              "result_ref": result_path.relative_to(root).as_posix() if result else "",
                              "status": "DELIVERED" if result.get("status") == "SUCCEEDED" else "DISPATCHED"})
    task_status = task_uses[-1]["status"] if task_uses else "PROPOSED" if task_ref else "NONE"
    state_path = root / work_ref(root, "state/director_state.json")
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    incorporated = route.get("incorporated_finding_versions")
    if incorporated is None:
        # Recover the versions available at that historical route, never the latest R view.
        route_parts = Path(route.get("ref", "")).parts
        route_turn = route_parts[1] if len(route_parts) > 1 else ""
        round_name = route_parts[2] if len(route_parts) > 2 else ""
        route_round = int(round_name[5:]) if round_name.startswith("round") and round_name[5:].isdigit() else 0
        incorporated = {row["finding_id"]: hashlib.sha256((root / row["source_ref"]).read_bytes()).hexdigest()
                        for row in finding_publications(root) if row.get("finding_id") and
                        (row.get("turn_id", "") < route_turn or
                         row.get("turn_id") == route_turn and row.get("round_index", 0) <= route_round)}
    versions_by_id = {row["id"]: row["version"] for row in records}
    unincorporated = [row["ref"] for row in finding_entries(root)
                     if incorporated.get(row["finding_id"]) != versions_by_id[row["finding_id"]]]
    assessment_path = root / state.get("last_directive_ref", "")
    assessment = json.loads(assessment_path.read_text()) if assessment_path.is_file() else {}
    # These are Planner declarations about uses, not approvals. Exact independent
    # receipts remain the sole authority; retaining a finding never closes its use.
    versions = {}
    for row in relations.get("relations", ()):
        for field in ("from_ref", "to_ref", "review_ref"):
            if row.get(field):
                ref = row[field].split("#", 1)[0]
                versions[ref] = hashlib.sha256((root / ref).read_bytes()).hexdigest()
    workspace.write_json("memory/research_index.json", {
        "authority": "SEARCH_CONTEXT_ONLY",
        "usage": "For objective g, select checked results WITH their conditions, exploratory records, and relevant open obligations. A checked result is not automatically an applicable premise. Read original manuscripts and bound reviews; keep missing application bridges open. Empty historical Kernel blockers do not mean the current target is complete.",
        "records": records,
        "research_records": research_records,
        "route_ref": route.get("ref", ""),
        "next_task_ref": task_ref if task_status == "PROPOSED" else "",
        "planner_suggestion": {"ref": task_ref, "status": task_status, "uses": task_uses,
                               "meaning": "Execution history only. DELIVERED does not close a mathematical obligation; consult bound verification."},
        "active_assignment_ref": state.get("pending_assignment_ref", ""),
        "execution_phase": state.get("phase", ""),
        "unincorporated_finding_refs": unincorporated,
        "last_route_assessment": {key: assessment.get(key, "" if key != "route_evidence_refs" else [])
                                  for key in ("route_assessment", "route_reason", "route_evidence_refs")},
        "relations_ref": relations_ref,
        "declared_relations": relations.get("relations", []),
        "relation_versions": versions,
        "open_obligations": relations.get("open_obligations", []),
        "obligations_status": "DECLARED_IN_ROUTE" if relations_ref else "READ_CURRENT_ROUTE_NOT_YET_INDEXED",
        "historical_kernel_relations_ref": "memory/verified_progress.json",
        "exploratory_notes": [p.relative_to(root).as_posix() for p in sorted((root / "memory/notes").glob("*.md"))],
    })


def sync_execution_memory(root: Path, state: dict) -> None:
    """Synchronize operational pointers after a state commit, without model work."""
    path = root / "memory/research_index.json"
    if not path.is_file():
        return
    value = json.loads(path.read_text())
    value.update(active_assignment_ref=state.get("pending_assignment_ref", ""),
                 execution_phase=state.get("phase", ""))
    directive = root / state.get("last_directive_ref", "")
    if directive.is_file():
        decision = json.loads(directive.read_text())
        value["last_route_assessment"] = {key: decision.get(key, "" if key != "route_evidence_refs" else [])
                                          for key in ("route_assessment", "route_reason", "route_evidence_refs")}
    from .workspace import _write_json_atomic

    _write_json_atomic(path, value)
