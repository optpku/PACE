"""Persist agent-authored notes and index their original evidence references."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from pace.runtime.context_state import empty_search_progress
from .results import FINDINGS_ARCHIVE_REF, FINDINGS_INDEX_REF, relocate_links
from .workspace import RunWorkspace, work_ref

logger = logging.getLogger(__name__)


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
    if published:
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
    lines = ["# 研究记忆", "", "记忆条目用于寻找研究上下文。复用时读取原始目标、假设、论证与核验记录；本轮验收以当前 literal target 为准。", "",
             "## 当前上下文", ""]
    for label, ref in (("当前问题", work_ref(root, "input/problem.md")),
                       ("当前目标", work_ref(root, "input/target_contract.json")),
                       ("输入索引", "memory/active_problem_dossier.json"),
                       ("累计核验关系与当前阻塞", "memory/verified_progress.json")):
        link = Path(os.path.relpath(root / ref, root / "memory")).as_posix()
        lines.append(f"- [{label}]({link})")
    lines.extend(["", "既往核验关系保留原始目标引用；跨轮使用需要重新检查范围与应用条件。"])
    latest_plan = ""
    for path in sorted(root.glob("work/turn*/stages/*/agent_result.json")):
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
