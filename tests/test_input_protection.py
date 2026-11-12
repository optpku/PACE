import pytest

from pace.runtime.input_protection import InputModifiedError, protected_inputs
from pace.runtime.output.results import publish_finding
from pace.runtime.output.workspace import RunWorkspace


def workspace(tmp_path):
    run = RunWorkspace.create(tmp_path, "topic")
    run.write_text(run.work_ref("input/problem.md"), "Prove the literal target.")
    run.write_json(run.work_ref("input/target_contract.json"), {"contract_id": "TARGET"})
    return run


def publish(run, number):
    source = run.work_ref(f"round{number:04d}/findings/bound.md")
    verification = run.work_ref(f"round{number:04d}/findings/bound.verification.json")
    run.write_text(source, f"# Bound\n\nThe independently checked version is {number}.\n")
    run.write_json(verification, {"stage_status": "PASS", "checked_manuscript": source})
    reference = publish_finding(
        run.root, source_ref=source, verification_ref=verification, round_index=number,
        publication={"action": "CREATE"} if number == 1 else {
            "action": "UPDATE", "finding_ref": "results/findings/R0001.md",
        },
    )
    return reference, source, verification


@pytest.mark.parametrize("explicit_input", [False, True])
def test_verified_update_is_readable_and_still_protected_during_the_next_call(tmp_path, explicit_input):
    run = workspace(tmp_path)
    reference, _, _ = publish(run, 1)
    first = protected_inputs(run.root, [reference] if explicit_input else [], run.root / "first")
    first.arm()
    first.check(run.root / "first/attempt")
    publish(run, 2)
    current = run.path(reference).read_bytes()
    second = protected_inputs(run.root, [reference] if explicit_input else [], run.root / "second")
    second.check(run.root / "second/attempt")
    assert run.path(reference).read_bytes() == current
    second.arm()
    run.path(reference).write_text("An unauthorized replacement during the model call.")
    with pytest.raises(InputModifiedError):
        second.check(run.root / "second/attempt")
    assert run.path(reference).read_bytes() == current


@pytest.mark.parametrize("protected", ["target", "manuscript", "verification"])
def test_later_call_does_not_accept_edits_to_immutable_inputs(tmp_path, protected):
    run = workspace(tmp_path)
    _, source, verification = publish(run, 1)
    refs = {"target": run.work_ref("input/problem.md"), "manuscript": source, "verification": verification}
    first = protected_inputs(run.root, [], run.root / "first")
    first.arm()
    first.check(run.root / "first/attempt")
    path = run.path(refs[protected])
    original = path.read_bytes()
    path.write_text("An unauthorized edit left before another call.")
    second = protected_inputs(run.root, [], run.root / "second")
    with pytest.raises(InputModifiedError):
        second.check(run.root / "second/attempt")
    assert path.read_bytes() == original
