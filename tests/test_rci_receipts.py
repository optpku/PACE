"""RCI receipt correction preserves reviews and validates only execution inputs."""

import json
from pathlib import Path

import pytest

from pace.agents.rci.phase_runner import RciPhaseError, RciPhaseRunner
from pace.runtime.llm import TransportSuccess
from pace.artifacts.calls import attempt_directory
from pace.artifacts.workspace import RunWorkspace
from pace.runtime.prompts import materialize_run_resources


REVIEW = "# Research review\n\nThe bounded question is worth exploring; no theorem is verified.\n"


class ReviewTransport:
    def __init__(self, directory, receipts, *, write_review=True):
        self.directory = directory
        self.receipts = iter(receipts)
        self.write_review = write_review
        self.prompts = []

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        self.prompts.append(prompt)
        if len(self.prompts) == 1 and self.write_review:
            (self.directory / "review.md").write_text(REVIEW)
        body = json.dumps(next(self.receipts))
        folder = attempt_directory(Path(workdir), ledger_context["call_ref"], 1)
        (folder / "response.txt").write_text(body)
        return TransportSuccess(
            text=body, invocation_id=f"review-test-{len(self.prompts)}", call_name=call_name,
            exit_code=0, terminal_event_type="turn.completed", accepted_output_source="file",
            output_schema_valid=True, attempt_count=1, artifact_dir=folder,
        )


@pytest.fixture
def review_task(tmp_path):
    ws = RunWorkspace.create(tmp_path, "rci-review")
    target = ws.work_ref("input/target_contract.json")
    seed = ws.work_ref("round1/seed.md")
    source = ws.work_ref("round1/source.md")
    empty = ws.work_ref("round1/scanned-source.txt")
    ws.write_text(ws.work_ref("input/problem.md"), "Investigate the stated local question.")
    ws.write_json(target, {"contract_id": "TARGET", "literal_target": "Investigate the local question."})
    ws.write_text(seed, "# Seed\n\nA precise local question with an unresolved bridge.")
    ws.write_text(source, "# Source note\n\nThe related literature leaves the stated bridge open.")
    ws.write_text(empty, "\f" * 20)
    materialize_run_resources(ws.root, resource_variant="standard")
    directory = ws.path(ws.work_ref("round1/rci/raw_seed_critic_research"))
    task = dict(
        phase="RAW_SEED_CRITIC_RESEARCH", workdir=ws.root, call_dir=directory,
        target_contract_id="TARGET", round_index=1,
        input_refs={"TARGET_ANCHOR": target, "SEED": seed, "SOURCE": (source, empty)},
        prompt_seed="Review only the seed's research value and readiness.",
    )
    return ws, directory, source, empty, task


def receipt(source):
    return {"action": "ACCEPT", "review_kind": "RESEARCH", "brief": "Proceed with exploration.",
            "dependency_refs": [source]}


def test_correction_accepts_auxiliary_context_and_reuses_the_committed_review(review_task):
    ws, directory, source, empty, task = review_task
    original = {**receipt(source), "dependency_refs": [source, empty]}
    corrected = {**receipt(source), "receipt_correction": {
        "removed_dependency_ref": empty, "reason": "The attempted source has no readable text.",
        "files_modified": [],
    }}
    transport = ReviewTransport(directory, [original, corrected])
    runner = RciPhaseRunner(llm=transport)

    result = runner.run(**task)

    assert len(transport.prompts) == 2
    assert result.payload["action"] == "ACCEPT"
    assert result.payload["dependency_refs"] == [source]
    assert result.payload["receipt_correction"] == corrected["receipt_correction"]
    assert result.payload["review_ref"] == (directory / "review.md").relative_to(ws.root).as_posix()
    assert json.loads((ws.root / result.output_ref).read_text()) == result.payload
    assert (directory / "completed_phase.json").is_file()
    assert (directory / "review.md").read_text() == REVIEW

    replay = runner.run(**task)

    assert replay.payload == result.payload
    assert len(transport.prompts) == 2
    assert (directory / "review.md").read_text() == REVIEW


@pytest.mark.parametrize("failure", ["missing_review", "empty_dependency", "invalid_action"])
def test_additional_context_does_not_bypass_required_rci_delivery(review_task, failure):
    _, directory, source, empty, task = review_task
    payload = {**receipt(source), "receipt_correction": {"files_modified": []}}
    if failure == "empty_dependency":
        payload["dependency_refs"] = [empty]
    elif failure == "invalid_action":
        payload["action"] = "DISPATCH"
    transport = ReviewTransport(directory, [payload, payload], write_review=failure != "missing_review")

    with pytest.raises(RciPhaseError) as error:
        RciPhaseRunner(llm=transport).run(**task)

    assert not (directory / "output.json").exists()
    assert not (directory / "completed_phase.json").exists()
    expected_pointer = {"missing_review": "/review_ref", "empty_dependency": "/dependency_refs",
                        "invalid_action": "/action"}[failure]
    assert any(row.get("json_pointer") == expected_pointer for row in error.value.diagnostics)
    assert len(transport.prompts) == (1 if failure == "missing_review" else 2)
    if failure != "missing_review":
        assert (directory / "review.md").read_text() == REVIEW
