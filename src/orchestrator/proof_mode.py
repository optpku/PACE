"""Finite direct-proof workflow sharing PACE transport and verification."""

from __future__ import annotations

import json
from pathlib import Path

from pace.contracts.proof.binding import (
    load_active_target,
    resolve_verification_binding,
)
from pace.orchestrator.director_led_adapters import (
    run_default_specialist,
)
from pace.orchestrator.director_verifier import build_default_verifier
from pace.runtime.output.events import publish_progress
from pace.runtime.prompts import (
    load_prompt_template,
    materialize_run_resources,
    validate_resource_snapshot,
)


def initialize_proof_state(ws, *, max_loops, resource_variant):
    """Commit the initial proof checkpoint before announcing the run."""
    ref = ws.work_ref("state/proof_state.json")
    state = (
        json.loads(ws.path(ref).read_text())
        if ws.path(ref).is_file()
        else {
            "schema_version": "proof_state/v2",
            "engine": "proof",
            "phase": "SURVEY",
            "turn_id": ws.turn_id,
            "loop_index": 1,
            "loops_started": 0,
            "max_loops": max_loops,
            "resource_variant": resource_variant,
            "completed_refs": [],
        }
    )
    if state["engine"] != "proof" or state["turn_id"] != ws.turn_id:
        raise ValueError("proof checkpoint belongs to a different engine or turn")
    if (type(state.get("loop_index")) is not int or type(state.get("max_loops")) is not int
            or not 1 <= state["loop_index"] <= state["max_loops"]):
        raise ValueError("invalid saved proof loop budget")
    ws.write_json(ref, state)
    return state


def run_proof(pipeline, ws, problem):
    """Resume only the committed phase; no Director or SMI is constructed."""
    ref = ws.work_ref("state/proof_state.json")
    state = json.loads(ws.path(ref).read_text())

    def save():
        ws.write_json(ref, state)

    save()
    resources = ws.path(ws.work_ref("resources"))
    if not resources.exists():
        materialize_run_resources(ws.root, resource_variant=state["resource_variant"])
    validate_resource_snapshot(resources, resource_variant=state["resource_variant"])
    target_id = load_active_target(ws.root)["contract_id"]
    from pace.runtime.sessions import budget_exhausted

    if state["phase"] in {"SURVEY", "PROVE"} and budget_exhausted(ws.root):
        state.update(phase="FINALIZE", stop_reason=budget_exhausted(ws.root))
        save()
    if state["phase"] == "SURVEY":
        output = ws.work_ref("orientation/survey.md")
        _run_task(
            pipeline,
            ws,
            state,
            "survey",
            output,
            0,
            "Perform the single initial Survey for proof mode. Preserve the closed question. "
            "Do not assess novelty or create research tasks.",
            target_id,
        )
        state["survey_ref"] = output
        state["phase"] = "PROVE"
        save()
    while state["phase"] in {"PROVE", "VERIFY"}:
        from pace.runtime.sessions import budget_exhausted

        if state["phase"] == "PROVE" and budget_exhausted(ws.root):
            state.update(phase="FINALIZE", stop_reason=budget_exhausted(ws.root))
            save()
            break
        index = state["loop_index"]
        directory = ws.work_ref(f"round{index}")
        output = f"{directory}/proof.md"
        result_ref = f"{directory}/agent_result.json"
        if state["phase"] == "PROVE":
            state["loops_started"] = index
            save()
            _run_task(
                pipeline,
                ws,
                state,
                "prover",
                output,
                index,
                f"Proof loop {index}/{state['max_loops']}. Write one complete proof of the same target. "
                "Use the initial Survey and retrieve additional sources when needed. On later loops revise the current proof "
                "using its verifier feedback; preserve sound arguments and repair the actual gap. "
                "Do not produce independent alternatives or a candidate pool.",
                target_id,
            )
            state["candidate_ref"] = output
            state["phase"] = "VERIFY"
            save()
        receipt_ref = f"{directory}/verification/receipt.json"
        if ws.path(receipt_ref).is_file():
            receipt = json.loads(ws.path(receipt_ref).read_text())
        else:
            result = json.loads(ws.path(result_ref).read_text())
            receipt = build_default_verifier(
                llm=pipeline._llm(), workdir=ws.root, target_contract_id=target_id
            )(
                context={
                    "target_contract_id": target_id,
                    "output_ref": output,
                    "input_refs": state["completed_refs"],
                    "previous_verification_ref": state.get(
                        "verification_checkpoint_ref", ""
                    ),
                },
                result=result,
                result_ref=result_ref,
                round_index=index,
            )
            if receipt["status"] == "VERIFIER_ERROR":
                raise RuntimeError(
                    "proof verification interrupted; resume this candidate"
                )
            ws.write_json(receipt_ref, receipt)
        verification_checkpoint = f"{directory}/verification/verification.json"
        ws.write_json(verification_checkpoint, {"verification": receipt})
        state["verification_checkpoint_ref"] = verification_checkpoint
        state["verification_ref"] = receipt["verification_ref"]
        state["verification_binding_ref"] = receipt["verification_binding_ref"]
        state["completed_refs"] = list(
            dict.fromkeys((*state["completed_refs"], receipt["verification_ref"]))
        )
        if receipt["status"] == "PASS" or index >= state["max_loops"]:
            state["phase"] = "FINALIZE"
            save()
            break
        state["loop_index"] += 1
        state["phase"] = "PROVE"
        save()
    return _finish(pipeline, ws, state, target_id)


def _run_task(pipeline, ws, state, agent, output, index, brief, target_id):
    task_id = "survey" if agent == "survey" else f"round-{index:04d}"
    result_ref = str(Path(output).parent / "agent_result.json")
    if ws.path(result_ref).is_file():
        result = json.loads(ws.path(result_ref).read_text())
        if result.get("status") == "SUCCEEDED":
            if not ws.path(output).is_file():
                raise ValueError(f"committed {agent} deliverable is missing: {output}")
            if agent == "survey":
                _complete_survey_sources(ws, output, result_ref, result)
            state["completed_refs"] = list(
                dict.fromkeys(
                    (
                        *state["completed_refs"],
                        output,
                        *result.get("published_refs", ()),
                    )
                )
            )
            ws.write_json(ws.work_ref("state/proof_state.json"), state)
            publish_progress(ws.root, task_id=task_id, stage=agent, status='completed', round_index=index)
            return
    directory = ws.path(output).parent
    directory.mkdir(parents=True, exist_ok=True)
    # Incomplete files are useful drafts, never committed candidates.
    if ws.path(output).exists():
        archive = directory / "interrupted_attempts"
        archive.mkdir(exist_ok=True)
        saved = archive / f"partial-{len(list(archive.iterdir())) + 1:04d}.md"
        ws.path(output).replace(saved)
        state["completed_refs"].append(saved.relative_to(ws.root).as_posix())
        ws.write_json(ws.work_ref("state/proof_state.json"), state)
    survey_sources = ()
    if agent == "prover" and state.get("survey_ref"):
        survey_result = ws.path(str(Path(state["survey_ref"]).parent / "agent_result.json"))
        if survey_result.is_file():
            survey_sources = tuple(json.loads(survey_result.read_text()).get("published_refs", ()))
    refs = list(
        dict.fromkeys(
            (
                ws.work_ref("input/problem.md"),
                ws.work_ref("input/target_contract.json"),
                ws.work_ref("input/problem_brief.md"),
                *((state.get("survey_ref", ""), *survey_sources, state.get("candidate_ref", ""),
                   state.get("verification_ref", ""), state.get("verification_checkpoint_ref", ""))
                  if agent == "prover" else state["completed_refs"]),
            )
        )
    )
    continuation = ws.work_ref("input/continuation_context.json")
    if ws.path(continuation).is_file():
        refs.extend((continuation, *json.loads(ws.path(continuation).read_text())["input_refs"]))
    if agent == "prover":
        refs = (
            *refs,
            ws.work_ref("resources/skills/direct-proving-and-gap-closure/SKILL.md"),
        )
    refs = tuple(x for x in refs if ws.path(x).is_file())
    role_ref = (
        "roles/proof_prover.md" if agent == "prover" else "roles/director_led_survey.md"
    )
    prompt = load_prompt_template(role_ref, workdir=ws.root)
    prompt += "\n\nHost execution mode: proof. " + brief
    prompt += "\nEarlier-turn sources, candidates and reviews are reading context only. Check their conditions again for this turn; historical acceptance never grants a new proof verdict."
    prompt += "\n## Files to read\n" + "\n".join(f"- `{ref}`" for ref in refs)
    prompt += f"\n## Deliverable\nWrite `{output}`.\n"
    from pace.runtime.prompts import prompt_root

    if agent == "prover":
        from pace.runtime.prompt_composer import write_dependency_manifest

        dependency_ref, dependencies = write_dependency_manifest(
            workdir=ws.root, output_ref=output, target_contract_id=target_id
        )
        refs = (*refs, dependency_ref)
        prompt += "\n" + dependencies
        for name in (
            "target_immutability",
            "scope_and_failure",
            "source_use",
            "numerical_boundary",
        ):
            guide = prompt_root(ws.root) / "guides" / f"{name}.md"
            prompt += "\n" + guide.read_text(encoding="utf-8")
    ws.write_json(
        str(Path(output).parent / "prompt_assembly.json"),
        {
            "assembly_source": "template",
            "execution_mode": "proof",
            "role": agent,
            "target_contract_id": target_id,
            "input_refs": refs,
            "output_ref": output,
        },
    )
    ws.write_text_atomic(str(Path(output).parent / "final_prompt.md"), prompt)
    publish_progress(ws.root, task_id=task_id, stage=agent, status='started', round_index=index)
    result = run_default_specialist(
        llm=pipeline._survey_llm() if agent == "survey" else pipeline._llm(),
        workdir=ws.root,
        prompt=prompt,
        agent_id=agent,
        input_refs=refs,
        output_ref=output,
        round_index=max(1, index),
        context={"target_contract_id": target_id, "task_id": task_id},
        on_accept=lambda accepted: ws.write_json(result_ref, accepted),
    )
    if result["status"] != "SUCCEEDED":
        publish_progress(ws.root, task_id=task_id, stage=agent, status='failed', round_index=index)
        raise RuntimeError(
            f"{agent} interrupted: {result.get('reason_code')}; resume this phase"
        )
    if agent == "survey":
        _complete_survey_sources(ws, output, result_ref, result)
    state["completed_refs"] = list(
        dict.fromkeys(
            (*state["completed_refs"], output, *result.get("published_refs", ()))
        )
    )
    publish_progress(ws.root, task_id=task_id, stage=agent, status='completed', round_index=index)


def _complete_survey_sources(ws, output, result_ref, result):
    from pace.runtime.output.sources import process_survey_sources

    source_refs = process_survey_sources(
        workdir=ws.root, report_ref=output,
        allow_network=result.get("external_retrieval_enabled") is True,
    )
    result["published_refs"] = list(dict.fromkeys((*result.get("published_refs", ()), *source_refs)))
    ws.write_json(result_ref, result)


def _finish(pipeline, ws, state, target_id):
    from pace.orchestrator.recovery import _project_terminal_result
    from pace.runtime.output.results import finding_entries, publish_finding

    report_ref = ws.work_ref("final_report.json")
    policy = json.loads(ws.path(ws.work_ref("state/research_execution_policy.json")).read_text())
    if not ws.path(report_ref).is_file():
        resolved = {"status": "FAILED", "proof_binding": {}, "artifact_refs": {}}
        if state.get("verification_binding_ref"):
            binding = json.loads(ws.path(state["verification_binding_ref"]).read_text())
            resolved = resolve_verification_binding(
                ws.root,
                verification_binding_ref=state["verification_binding_ref"],
                proof_id=binding["proof_id"],
                proof_ref=binding["proof_ref"],
                target_contract_id=target_id,
                verification_ref=binding["verification_ref"],
            )
        passed = resolved["status"] == "PASS"
        report = {
            "schema_version": "final_report/v8",
            "problem_id": ws.problem_id,
            "run_status": "completed",
            "result": "PASS" if passed else "FAILED",
            "mathematical_status": "VERIFIED" if passed else "OPEN",
            "answer_mode": resolved.get("final_result", {}).get("answer_mode", ""),
            "terminal_kind": "PROOF",
            "execution_mode": "proof",
            "mode_source": policy["mode_source"],
            "mode_reason": policy["mode_reason"],
            "proof_loops_used": state["loops_started"],
            "max_proof_loops": state["max_loops"],
            "termination_owner": "PROOF",
            "reason_code": "candidate_verified"
            if passed
            else state.get("stop_reason", "proof_budget_exhausted"),
            "target_contract_id": target_id,
            "target_contract_unavailable": False,
            "proof_binding": resolved["proof_binding"],
            "artifact_refs": {
                **resolved["artifact_refs"],
                "target_contract": ws.work_ref("input/target_contract.json"),
                "report": ws.work_ref("outcome.md"),
            },
            "publication_status": "NO_PUBLICATION",
        }
        if passed:
            kernel = json.loads(ws.path(report["artifact_refs"]["kernel"]).read_text())
            publish_finding(
                ws.root, source_ref=report["artifact_refs"]["proof"],
                verification_ref=report["artifact_refs"]["verification"],
                round_index=state["loop_index"], publication=kernel["publication"],
            )
            report["publication_status"] = "PUBLISHED"
        report["artifact_refs"]["findings"] = [row["ref"] for row in finding_entries(ws.root)]
        ws.write_json(report_ref, report)
    report = json.loads(ws.path(report_ref).read_text())
    outcome = ws.work_ref("outcome.md")
    ws.write_text_atomic(outcome, "# Mathematical outcome\n\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    publish_progress(ws.root, task_id='proof-finished', stage='run', status='finished', final_report=report, execution_mode='proof', proof_loops_used=state['loops_started'], max_proof_loops=state['max_loops'])
    return _project_terminal_result(ws, report)
