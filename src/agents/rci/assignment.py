"""Deliver one Director assignment to RCI and publish the original work."""

import json
import os
from pathlib import Path

from pace.agents.director.dispatch import SpecialistAdapterResult
from pace.artifacts.workspace import _write_text_atomic, work_ref
from pace.runtime.semantic import diagnostic_messages

from .phase_runner import RciPhaseRunner
from .pipeline import RciPipeline


def build_rci_handler(*, llm, workdir):
    root = Path(workdir)

    def run(**kwargs):
        output = root / kwargs["output_ref"]
        attempt = output.parent / "rci" / "attempt_001"
        outcome_path = attempt / "outcome.json"
        saved = json.loads(outcome_path.read_text()) if outcome_path.is_file() else None
        if saved is None or saved["outcome"] == "INTERFACE_FAILED":
            context = kwargs["context"]
            decision = kwargs["decision"]
            anchor = work_ref(root, "input/target_contract.json")
            excluded = {
                anchor,
                context["assignment_ref"],
                work_ref(root, "input/request.md"),
                work_ref(root, "state/proof_loop_budget.json"),
                str(Path(context["assignment_ref"]).with_name("context.json")),
                decision.research_problem_ref,
                context["verified_progress_ref"],
            }
            # The Director has already frozen these inputs and snapshotted
            # mutable references. Its navigation and archives are separate.
            selected = tuple(
                ref
                for ref in kwargs["input_refs"]
                if ref not in excluded and (root / ref).is_file()
            )
            related = tuple(ref for ref in selected if Path(ref).name == "candidate.md")
            mechanisms = {
                ref: _mechanism_identity(root, ref)
                for ref in selected
                if Path(ref).name == "mechanism.md"
            }
            sources = tuple(
                ref for ref in selected if ref not in related and ref not in mechanisms
            )
            independent = _independent_source_refs(
                root, context["completed_result_refs"]
            )
            RciPipeline(phase_runner=RciPhaseRunner(llm=llm)).run(
                workdir=root,
                round_root=attempt,
                round_index=kwargs["round_index"],
                target_contract_id=context["target_contract_id"],
                target_anchor_ref=anchor,
                work_item_ref=context["assignment_ref"],
                research_operation=decision.research_operation,
                research_problem_ref=decision.research_problem_ref,
                source_refs=sources,
                independent_source_refs=tuple(
                    ref for ref in sources if ref in independent
                ),
                related_candidate_refs=related,
                mechanisms=mechanisms,
                task_brief=decision.prompt_brief,
            )
            saved = json.loads(outcome_path.read_text())

        outcome_ref = outcome_path.relative_to(root).as_posix()
        refs = list(dict.fromkeys((outcome_ref, *saved["published_refs"])))
        if saved["outcome"] == "INTERFACE_FAILED":
            failure = saved["failure"]
            return {
                **SpecialistAdapterResult(
                    agent_id="rci",
                    status="INTERFACE_FAILED",
                    failure_class="HOST_INTERFACE",
                    reason_code=saved["reason"],
                    diagnostics_ref=outcome_ref,
                ).to_json(),
                "diagnostic_refs": refs,
                "diagnostics": failure["diagnostics"],
                "message": "RCI 接口失败："
                + "；".join(diagnostic_messages([failure]) or [failure["message"]]),
            }

        def link(ref):
            path = Path(ref)
            return f"- [{path.parent.name}/{path.name}]({Path(os.path.relpath(root / ref, output.parent)).as_posix()})"

        sections = [
            "# RCI research handoff",
            "Authority: SEARCH_CONTEXT_ONLY",
            f"Outcome: {saved['outcome']} ({saved['reason']})",
        ]
        if saved["candidate_ref"]:
            sections.append("## Current question\n" + link(saved["candidate_ref"]))
        if saved["evidence_refs"]:
            latest = saved["evidence_refs"][-1]
            sections.append("## Latest original research note\n" + link(latest))
        if saved["failure"]:
            sections.append("## Missing input\n" + saved["failure"]["message"])
        sections.append("## Original phase files\n" + "\n".join(map(link, refs)))
        _write_text_atomic(output, "\n\n".join(sections) + "\n")

        result = {
            **SpecialistAdapterResult(
                agent_id="rci",
                status="SUCCEEDED",
                deliverable_ref=kwargs["output_ref"],
                reason_code=saved["reason"],
            ).to_json(),
            "published_refs": refs,
        }
        if kwargs.get("on_accept"):
            kwargs["on_accept"](result)
        return result

    return run


def _mechanism_identity(root: Path, ref: str) -> str:
    receipt = (root / ref).with_name("output.json")
    if receipt.is_file():
        record = json.loads(receipt.read_text())
        identity = record.get("mechanism_id")
        if (
            record.get("mechanism_ref") == ref
            and identity
            and (root / identity).is_file()
        ):
            return identity
    return ref


def _independent_source_refs(root, completed_result_refs):
    """Identify acquired source material among the supplied files."""

    refs = {work_ref(root, "input/problem.md")}
    for ref in completed_result_refs:
        if Path(ref).name != "agent_result.json":
            continue
        result = json.loads((root / ref).read_text())
        if result.get("agent_id") != "survey" or result.get("status") != "SUCCEEDED":
            continue
        report_ref = result["deliverable_ref"]
        refs.add(report_ref)
        source_dir = Path(report_ref).parent / "sources"
        refs.update(
            item
            for item in result.get("published_refs", ())
            if Path(item).is_relative_to(source_dir)
        )
    return refs
