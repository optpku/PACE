"""Connect the existing SMI phase graph to one Director-owned assignment."""

import json
import os
from pathlib import Path

from pace.agents.research.smi.phase_runner import SmiPhaseRunner
from pace.agents.research.smi.pipeline import SmiPipeline
from pace.orchestrator.director_led_adapters import SpecialistAdapterResult
from pace.runtime.output.workspace import stage_ref, work_ref
from pace.runtime.output.results import relocate_links


def build_smi_handler(*, llm, workdir, phase_runner=None):
    root = Path(workdir)

    def run(**kwargs):
        index = kwargs["round_index"]
        output = root / kwargs["output_ref"]
        round_root = output.parent / "smi"
        # Republish completed research handoffs; a Host failure remains retryable
        # within this same assignment and retains its graph as research context.
        attempts = sorted(round_root.glob("attempt_*"))
        complete = next(
            (p for p in reversed(attempts) if (p / "outcome.json").is_file()), None
        )
        resume_attempt = attempts[-1] if attempts and complete is None else None
        if complete is not None:
            previous = json.loads((complete / "outcome.json").read_text())
            if previous["outcome"] == "INTERFACE_FAILED" and not _context_gap(
                root, previous
            ):
                resume_attempt = complete
                complete = None
        if complete is None:
            attempt = resume_attempt or round_root / f"attempt_{len(attempts) + 1:03d}"
            if (attempt / "outcome.json").is_file():
                failures = list(attempt.glob("failed_outcome_*.json"))
                (attempt / "outcome.json").replace(
                    attempt / f"failed_outcome_{len(failures) + 1:03d}.json"
                )
            previous_attempts = [prior for prior in attempts if prior != attempt]
            assignment_ref = f"{stage_ref(index, workdir=root)}/assignment/task.json"
            # Director-selected evidence is distinct from Host navigation and budgets.
            excluded = {work_ref(root, "input/target_contract.json"),
                        work_ref(root, "input/problem.md"), work_ref(root, "input/request.md"),
                        work_ref(root, "state/proof_loop_budget.json"),
                        "results/findings/README.md", "results/findings/archive.md"}
            inputs = tuple(ref for ref in kwargs["input_refs"]
                           if ref not in excluded and not ref.endswith("/progress_snapshot.json")
                           and "/resources/" not in ref)
            previous_outputs = tuple(
                p.relative_to(root).as_posix()
                for prior in previous_attempts
                for p in sorted(prior.rglob("output.json"))
            )
            previous_outcomes = tuple(
                (prior / "outcome.json").relative_to(root).as_posix()
                for prior in previous_attempts
                if (prior / "outcome.json").is_file()
            )
            frozen_refs_path = attempt / "source_snapshot.json"
            attempt.mkdir(parents=True, exist_ok=True)
            independent_originals = _independent_source_refs(
                root, kwargs["context"].get("completed_result_refs", ())
            )
            if frozen_refs_path.is_file():
                snapshot = json.loads(frozen_refs_path.read_text())
                sources = tuple(snapshot["refs"])
                independent_sources = tuple(snapshot["independent_refs"])
            else:
                from pace.runtime.output.workspace import _write_json_atomic

                sources, provenance = [], []
                for original in dict.fromkeys(
                    (*inputs, *(ref for ref in independent_originals if ref in inputs), *previous_outputs)
                ):
                    source = (root / original).resolve()
                    if not source.is_file() or not source.is_relative_to(
                        root.resolve()
                    ):
                        continue
                    destination = (
                        attempt / "source_snapshot" / source.relative_to(root.resolve())
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if source.suffix == ".md":
                        destination.write_text(
                            relocate_links(
                                source.read_text(encoding="utf-8"), source, destination
                            ),
                            encoding="utf-8",
                        )
                    else:
                        destination.write_bytes(source.read_bytes())
                    reference = destination.relative_to(root).as_posix()
                    sources.append(reference)
                    provenance.append(
                        {"original_ref": original, "snapshot_ref": reference}
                    )
                independent_sources = tuple(
                    row["snapshot_ref"] for row in provenance
                    if row["original_ref"] in independent_originals
                )
                _write_json_atomic(
                    frozen_refs_path,
                    {
                        "refs": sources,
                        "provenance": provenance,
                        "independent_refs": independent_sources,
                        "authority": "SEARCH_CONTEXT_ONLY",
                    },
                )
            outcome_ref = SmiPipeline(
                phase_runner=phase_runner or SmiPhaseRunner(llm=llm)
            ).run(
                workdir=root,
                round_root=attempt,
                round_index=index,
                target_contract_id=kwargs["context"]["target_contract_id"],
                work_item_id=f"{Path(work_ref(root)).name}-SMI-{index:04d}",
                target_anchor_ref=work_ref(workdir, "input/target_contract.json"),
                work_item_ref=assignment_ref,
                verified_progress_ref=kwargs["context"].get(
                    "verified_progress_ref", ""
                ),
                source_refs=tuple(sources),
                independent_source_refs=independent_sources,
                blocked_context_refs=previous_outcomes,
                research_operation=kwargs["decision"].research_operation,
                research_problem_ref=kwargs["decision"].research_problem_ref,
                prior_accepted_refs=_prior_accepted_task_refs(
                    root,
                    kwargs["context"].get("completed_result_refs", ()),
                ),
                prior_batch_refs=_prior_accepted_task_refs(
                    root, kwargs["context"].get("completed_result_refs", ()), batches=True
                ),
            )
            complete = root / Path(outcome_ref).parent
        outcome_ref = (complete / "outcome.json").relative_to(root).as_posix()
        outcome = json.loads((root / outcome_ref).read_text())
        recoverable = _context_gap(root, outcome)
        handed_back = "explorer_handoff" in outcome["reasons"]
        phase_outputs = sorted(complete.rglob("output.json"))
        refs = [
            outcome_ref,
            *(p.relative_to(root).as_posix() for p in phase_outputs),
            *outcome["candidate_failure_refs"],
            *((outcome["scope_reviews_ref"],) if outcome.get("scope_reviews_ref") else ()),
        ]
        changes = {}
        for path in phase_outputs:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            refs.extend(value for key, value in receipt.items()
                        if key.endswith("_ref") and isinstance(value, str)
                        and value and (root / value).is_file())
            if path.parent.name.startswith("frontier_explorer"):
                changes[path.parent.parent] = receipt["change_ref"]
        refs.extend(path.relative_to(root).as_posix()
                    for path in sorted((output.parent / "findings").glob("*.md")))
        if outcome["task_batch_ref"]:
            refs.append(outcome["task_batch_ref"])
            batch = json.loads(
                (root / outcome["task_batch_ref"]).read_text(encoding="utf-8")
            )
            for task in (*batch["tasks"], *batch["deferred_tasks"]):
                refs.extend((task["candidate_ref"], *task["context_refs"]))
        refs = list(dict.fromkeys(refs))
        if outcome["outcome"] == "INTERFACE_FAILED" and not recoverable:
            from pace.runtime.semantic import diagnostic_messages

            failures = [json.loads((root / ref).read_text()) for ref in outcome["candidate_failure_refs"]]
            details = diagnostic_messages(failures)
            return {
                **SpecialistAdapterResult(
                    agent_id="smi",
                    status="INTERFACE_FAILED",
                    failure_class="HOST_INTERFACE",
                    reason_code="smi_interface_failed",
                    diagnostics_ref=outcome_ref,
                ).to_json(),
                "diagnostic_refs": refs,
                "message": "SMI 接口失败：" + "；".join(details),
                "diagnostics": failures,
            }
        # Publish persisted graph artifacts, not prompt/transport mirrors. The
        # Director selects exact refs; no second scheduler or child proof loop.
        text = "# SMI research handoff\n\nAuthority: SEARCH_CONTEXT_ONLY\n\n"
        text += f"Outcome: {outcome['outcome']}\n\n"
        if recoverable:
            text += "Work item status: READY. Repair the missing input context before a later bounded retry.\n\n"
        elif handed_back:
            text += "Work item status: READY. The question is preserved; this bounded attempt returned its unresolved work to the Director. Use the original gap and needed input below to choose the next assignment; do not repeat an unchanged attempt. This is not a mathematical rejection.\n\n"
        text += "All proposed tasks and probe resolutions require downstream proof and independent verification.\n\n"
        for ref in changes.values():
            text += f"## Original exploration handoff\n\nSource: `{ref}`\n\n"
            text += relocate_links(
                (root / ref).read_text(encoding="utf-8").strip(), root / ref, output,
            ) + "\n\n"
        text += "## Original task and review files\n"
        text += (
            "\n".join(
                f"- [{Path(ref).name}]({Path(os.path.relpath(root / ref, output.parent)).as_posix()})"
                for ref in refs
            )
            + "\n"
        )
        output.write_text(text, encoding="utf-8")
        return {
            **SpecialistAdapterResult(
                agent_id="smi",
                status="SUCCEEDED",
                deliverable_ref=kwargs["output_ref"],
                reason_code=recoverable or ("explorer_handoff" if handed_back else ""),
                diagnostics_ref=outcome_ref if recoverable or handed_back else "",
            ).to_json(),
            "published_refs": refs,
        }

    return run


def _independent_source_refs(root, completed_result_refs):
    """Separate committed source acquisition from internal research receipts."""

    refs = [work_ref(root, "input/problem.md")]
    for ref in completed_result_refs:
        if Path(ref).name != "agent_result.json":
            continue
        result = json.loads((root / ref).read_text(encoding="utf-8"))
        if result.get("agent_id") != "survey" or result.get("status") != "SUCCEEDED":
            continue
        report_ref = result.get("deliverable_ref", "")
        if not report_ref:
            continue
        refs.append(report_ref)
        source_directory = (root / report_ref).resolve().parent / "sources"
        refs.extend(
            item for item in result.get("published_refs", ())
            if (root / item).resolve().is_relative_to(source_directory)
        )
    return tuple(dict.fromkeys(refs))


def _context_gap(root, outcome):
    reasons = list(outcome["reasons"])
    if outcome["outcome"] != "INTERFACE_FAILED":
        for ref in outcome["candidate_failure_refs"]:
            diagnostic = json.loads((root / ref).read_text())
            reasons.append(diagnostic.get("reason_code", ""))
            reasons.extend(row.get("reason_code", "") for row in diagnostic.get("diagnostics", ()))
    return next((reason for reason in reasons
                 if reason in {"required_input_missing", "required_input_file_missing"}), "")


def _prior_accepted_task_refs(root, completed_result_refs, *, batches=False):
    """Read acceptance receipts from the parent's committed task snapshot."""

    refs = []
    for ref in completed_result_refs:
        if Path(ref).name != "agent_result.json":
            continue
        result = json.loads((root / ref).read_text(encoding="utf-8"))
        if result.get("agent_id") != "smi" or result.get("status") != "SUCCEEDED":
            continue
        for published in result.get("published_refs", ()):
            if Path(published).name != "outcome.json":
                continue
            outcome = json.loads((root / published).read_text(encoding="utf-8"))
            if batches:
                if outcome.get("scope_reviews_ref") and (root / outcome["scope_reviews_ref"]).is_file():
                    refs.append(outcome["scope_reviews_ref"])
                if outcome.get("task_batch_ref") and (root / outcome["task_batch_ref"]).is_file():
                    refs.append(outcome["task_batch_ref"])
                continue
            refs.extend(ref for ref in outcome["accepted_task_refs"]
                        if (root / ref).is_file())
    return tuple(dict.fromkeys(refs))
