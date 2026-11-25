"""Publish independently checked originals into problem-level cumulative results."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pace.artifacts.results import (
    FINDINGS_ARCHIVE_REF,
    FINDINGS_CATALOG_REF,
    FINDINGS_INDEX_REF,
    FINDINGS_STATE_REF,
    _read_state,
    committed_finding_refs,
    finding_entries,
)
from pace.artifacts.results import publish_finding as persist_finding
from pace.artifacts.workspace import RunWorkspace, _write_text_atomic, work_ref

logger = logging.getLogger(__name__)


def _finding_link(root: Path, row: dict, document: Path) -> str:
    with (root / row["ref"]).open(encoding="utf-8") as manuscript:
        title = next(
            (line[2:].strip() for line in manuscript if line.startswith("# ")), ""
        )
    label = row["finding_id"] + (f" · {title}" if title else "")
    link = Path(os.path.relpath(root / row["ref"], document.parent)).as_posix()
    return f"- [{label}]({link})"


def refresh_findings_indexes(workdir: Path) -> None:
    """Refresh current entries and immutable sources without replacing editorial navigation."""
    root = Path(workdir)
    state = _read_state(root / FINDINGS_STATE_REF, {"entries": [], "publications": []})
    document = root / FINDINGS_ARCHIVE_REF
    lines = [
        "# 核验原稿档案",
        "",
        "按用户轮次保留每次核验的固定原稿及核验记录。成果编号指向持续完善的结果；历史原稿不会随当前成果更新。",
        "",
    ]
    turns: dict[str, list[dict]] = {}
    for row in state["publications"]:
        turns.setdefault(row["turn_id"], []).append(row)
    if not turns:
        lines.append("尚无已保存的核验原稿。")
    for turn_id, rows in turns.items():
        lines.extend([f"## {turn_id}", ""])
        for row in rows:
            source = root / row["source_ref"]
            title = next(
                (
                    line[2:].strip()
                    for line in source.read_text(encoding="utf-8").splitlines()
                    if line.startswith("# ")
                ),
                source.name,
            )
            label = (
                f"{row['finding_id']} · " if row["finding_id"] else "支撑材料或报告 · "
            )
            original = Path(os.path.relpath(source, document.parent)).as_posix()
            verification = Path(
                os.path.relpath(root / row["verification_ref"], document.parent)
            ).as_posix()
            lines.append(f"- [{label}{title}]({original}) · [核验记录]({verification})")
        lines.append("")
    _write_text_atomic(document, "\n".join(lines).rstrip() + "\n")
    catalog = root / FINDINGS_CATALOG_REF
    rows = [_finding_link(root, row, catalog) for row in finding_entries(root)]
    _write_text_atomic(
        catalog, "# 当前成果条目\n\n" + "\n".join(rows or ["尚无独立成果。"]) + "\n"
    )
    index = root / FINDINGS_INDEX_REF
    if not index.is_file():
        _write_text_atomic(
            index,
            "# 研究成果\n\n每个编号表示一项持续完善的数学结果。"
            "主题说明与适用范围可在此整理，发布新稿不会覆盖本页。\n\n"
            "- [当前成果条目](catalog.md)\n- [核验原稿档案](archive.md)\n",
        )


def publish_finding(workdir: Path, **kwargs) -> str:
    ref = persist_finding(workdir, **kwargs)
    refresh_findings_indexes(workdir)
    return ref


def publish_stage_findings(
    *, workdir: Path, completed_result_refs, accepted_candidate_ref: str = ""
) -> None:
    """Publish only the receipts already delivered by independent turn-level checks."""
    from pace.contracts.research.memory import check_finding_record
    from pace.orchestrator.memory import (
        refresh_application_memory,
        refresh_memory_index,
        update_research_costs,
    )

    root = Path(workdir)
    for result_ref in dict.fromkeys(completed_result_refs):
        if Path(result_ref).name != "agent_result.json":
            continue
        result = json.loads((root / result_ref).read_text())
        if (
            result.get("status") != "SUCCEEDED"
            or result.get("deliverable_ref") == accepted_candidate_ref
        ):
            continue
        folder = root / Path(result_ref).parent
        for ref in committed_finding_refs(root, result_ref):
            draft = root / ref
            receipt = draft.with_suffix(".verification.json")
            if not receipt.is_file():
                continue
            record = json.loads(receipt.read_text())
            target = json.loads(
                (root / work_ref(root, "input/target_contract.json")).read_text()
            )
            check_finding_record(root, ref, target["contract_id"], record)
            if record["verdict"].get("stage_status") == "PASS":
                assignment = json.loads((folder / "assignment/task.json").read_text())
                publish_finding(
                    root,
                    source_ref=draft.relative_to(root).as_posix(),
                    verification_ref=receipt.relative_to(root).as_posix(),
                    round_index=assignment["round_index"],
                    publication=record["verdict"]["publication"],
                )
    refresh_application_memory(root)
    update_research_costs(root)
    refresh_memory_index(root)


def publish_terminal_report(ws: RunWorkspace, report: dict[str, Any]) -> dict[str, Any]:
    """Publish terminal authority and retain cumulative verified findings."""
    refs = report["artifact_refs"]
    if report.get("operational_reason_code") == "finding_publication_incomplete":
        report.update(
            run_status="completed",
            operational_failure_owner="",
            operational_reason_code="",
        )
        report.pop("finding_publication_diagnostics", None)
    report["publication_status"] = (
        "REPORT_ONLY"
        if report.get("execution_mode") == "research"
        else "NO_PUBLICATION"
    )
    if report["result"] == "PASS":
        try:
            from pace.contracts.proof.binding import (
                load_active_target,
                resolve_verification_binding,
            )

            binding_ref = refs.get("verification_binding") or report.get(
                "proof_binding", {}
            ).get("verification_binding_ref")
            if not binding_ref:
                sibling = Path(refs["verification"]).with_name(
                    "verification_binding.json"
                )
                if ws.path(sibling.as_posix()).is_file():
                    binding_ref = sibling.as_posix()
            if binding_ref:
                resolved = resolve_verification_binding(
                    ws.root,
                    verification_binding_ref=binding_ref,
                    proof_ref=refs["proof"],
                    verification_ref=refs["verification"],
                    target_contract_id=load_active_target(ws.root)["contract_id"],
                )
                if (
                    resolved["status"] != "PASS"
                    or resolved["artifact_refs"]["kernel"] != refs["kernel"]
                ):
                    raise ValueError(
                        "terminal publication differs from the verified proof"
                    )
                kernel = resolved["kernel"]
            else:
                # Older terminal records identify the exact proof and Kernel in the verifier report.
                verifier = json.loads(ws.path(refs["verification"]).read_text())
                if (
                    verifier["proof_ref"] != refs["proof"]
                    or verifier["kernel_ref"] != refs["kernel"]
                ):
                    raise ValueError(
                        "terminal publication references another proof or Kernel"
                    )
                kernel = json.loads(ws.path(refs["kernel"]).read_text())
            if (
                kernel.get("stage_status") != "PASS"
                or kernel.get("publication_eligible") is not True
            ):
                raise ValueError("Kernel did not authorize terminal publication")
            published_ref = publish_finding(
                ws.root,
                source_ref=refs["proof"],
                verification_ref=refs["verification"],
                round_index=report.get("director_state", {}).get(
                    "round_index", report.get("proof_loops_used", 0)
                ),
                publication=kernel["publication"],
            )
            report["publication_status"] = (
                "PUBLISHED" if published_ref else "NO_PUBLICATION"
            )
        except Exception as exc:
            report.update(
                publication_status="INCOMPLETE",
                run_status="completed_with_operational_error",
                operational_failure_owner="INTERFACE",
                operational_reason_code="finding_publication_incomplete",
            )
            report["finding_publication_diagnostics"] = [str(exc)]
            logger.warning("Verified final finding publication failed", exc_info=True)
    refs["findings"] = [row["ref"] for row in finding_entries(ws.root)]
    from pace.contracts.research.memory import enabled

    if enabled(ws.root) and ws.path("memory/research_index.json").is_file():
        refs["research_memory"] = "memory/research_index.json"
        refs["research_memory_navigation"] = "memory/README.md"
    return report
