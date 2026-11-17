"""Role instructions and native sessions through the production call boundary."""

import json
import subprocess
from pathlib import Path

import pytest

from pace.agents.orientation import build_problem_extraction_prompt
from pace.agents.research.global_director import build_global_director_prompt
from pace.runtime.agent_environment import prepare_agent_environments, role_home
from pace.runtime.llm import CodexLLM
from pace.runtime.output.workspace import RunWorkspace
from pace.runtime.prompt_composer import compose_prompt
from pace.runtime.prompt_policy import build_prompt_assembly
from pace.runtime.prompts import materialize_run_resources, prompt_root
from pace.runtime.semantic import begin_artifact_call


@pytest.fixture
def role_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PACE_CODEX_ENV_ROOT", str(tmp_path / ".nl-proof-acp"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "credentials"))
    ws = RunWorkspace.create(tmp_path / "runs", "problem")
    ws.write_text(ws.work_ref("input/problem.md"), "Investigate integer addition.")
    materialize_run_resources(ws.root, resource_variant="standard")
    prepare_agent_environments(ws.root, mode="research")
    return ws


@pytest.mark.parametrize("variant", ["standard", "weak"])
def test_role_templates_keep_current_values_only_in_task(tmp_path, monkeypatch, variant):
    monkeypatch.setenv("PACE_CODEX_ENV_ROOT", str(tmp_path / ".nl-proof-acp"))
    ws = RunWorkspace.create(tmp_path / "runs", "problem")
    materialize_run_resources(ws.root, resource_variant=variant)
    original = {p: p.read_bytes() for p in prompt_root(ws.root).rglob("*.md")}
    prepare_agent_environments(ws.root, mode="research")
    homes = list((tmp_path / ".nl-proof-acp/runs/problem/turn001").rglob("AGENTS.md"))
    assert len(homes) == 16
    assert not list((tmp_path / ".nl-proof-acp").rglob("sessions"))
    task = build_global_director_prompt(workdir=ws.root, target_contract_id="CURRENT-TARGET",
                                       input_refs=("work/turn001/new-evidence.md",),
                                       control_context="Current rounds remaining: 7")
    instructions = (role_home(ws.root, "GLOBAL_RESEARCH_DIRECTOR") / "AGENTS.md").read_text()
    assert "# Global Research Director" in instructions
    assert "TASK.control_context" in instructions
    assert "CURRENT-TARGET" not in instructions
    assert "Current rounds remaining: 7" not in instructions
    assert "CURRENT-TARGET" in task and "Current rounds remaining: 7" in task
    assert "# Global Research Director" not in task
    assert "{{" not in instructions
    extraction = build_problem_extraction_prompt("An exact new request.", workdir=ws.root)
    assert "An exact new request." in extraction
    assert "# Literal Target Locations" not in extraction
    assert all(p.read_bytes() == content for p, content in original.items())
    prepare_agent_environments(ws.root, mode="proof")
    direct = (role_home(ws.root, "WHOLE_PROVER") / "AGENTS.md").read_text()
    assert "# Direct Proof Prover" in direct
    assert "# Director-led Prover" not in direct


def test_composer_passes_task_and_evidence_without_fixed_instructions(role_workspace):
    ws = role_workspace
    evidence = ws.work_ref("input/problem.md")
    assembly = build_prompt_assembly(workdir=ws.root, stage="SPECIALIST", role="DIRECTOR_LED_RESEARCH",
        mode="SPECIALIST", target_contract_id="CURRENT", required_file_refs=(evidence,),
        prompt_seed="Derive the assigned local relation.", output_artifact_ref=ws.work_ref("round1/research.md"))
    task = compose_prompt(llm=None, workdir=ws.root, assembly=assembly,
                          input_refs=(evidence,), call_name="compose").final_prompt
    assert "Derive the assigned local relation." in task
    assert evidence in task
    instructions = (role_home(ws.root, "RESEARCH") / "AGENTS.md").read_text()
    assert "# Director-led Research Specialist" in instructions
    assert "# Transport Boundary" in instructions
    assert "# Standalone research findings" in instructions
    assert "# Director-led Research Specialist" not in task
    assert "# Transport Boundary" not in task
    assert "resources/prompts/" not in task


def native_process(monkeypatch, *, interrupt_first=False):
    calls = []

    def run(cmd, *, cwd, env, input, stdout, stderr, timeout):
        index = len(calls)
        thread = cmd[3] if cmd[2] == "resume" else f"native-thread-{index}"
        calls.append((cmd, Path(env["CODEX_HOME"]), thread, Path(cwd)))
        stdout.write(json.dumps({"type": "thread.started", "thread_id": thread}) + "\n")
        stdout.flush()
        if interrupt_first and index == 0:
            raise KeyboardInterrupt
        Path(cmd[cmd.index("-o") + 1]).write_text("Completed the assigned task.")
        stdout.write(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("pace.runtime.llm._run_cli_process", run)
    return calls


def invoke(ws, role, name):
    return begin_artifact_call(CodexLLM(max_retries=0), "Complete this bounded task.",
        workdir=ws.root, call_name=name, role=role, stage="TEST", input_refs=())


def test_director_resumes_while_specialists_start_new_native_sessions(role_workspace, monkeypatch):
    ws = role_workspace
    calls = native_process(monkeypatch)
    results = [invoke(ws, role, f"call-{index}") for index, role in enumerate(
        ("GLOBAL_RESEARCH_DIRECTOR", "GLOBAL_RESEARCH_DIRECTOR", "SURVEY", "SURVEY"))]
    assert all(result.transport_status == "SUCCESS" for result in results)
    assert calls[0][2] == calls[1][2]
    assert calls[2][2] != calls[3][2]
    assert calls[2][1] == calls[3][1] == role_home(ws.root, "SURVEY")
    assert calls[0][1] != calls[2][1]
    assert all("--ephemeral" not in cmd and cwd == ws.root for cmd, _, _, cwd in calls)
    assert all("project_doc_max_bytes=0" not in cmd for cmd, _, _, _ in calls)
    assert calls[1][0][2] == "resume" and "-C" not in calls[1][0]
    for result, (_, home, thread, _) in zip(results, calls):
        record = json.loads((ws.root / result.call_ref).read_text())
        assert record["agent_home"] == str(home)
        assert record["thread_id"] == thread
    previous_home = calls[0][1]
    ws.start_turn()
    materialize_run_resources(ws.root, resource_variant="standard")
    prepare_agent_environments(ws.root, mode="research")
    invoke(ws, "GLOBAL_RESEARCH_DIRECTOR", "new-turn")
    assert calls[-1][0][2] != "resume"
    assert calls[-1][1] != previous_home


def test_interrupted_director_retains_native_session(role_workspace, monkeypatch):
    calls = native_process(monkeypatch, interrupt_first=True)
    with pytest.raises(KeyboardInterrupt):
        invoke(role_workspace, "GLOBAL_RESEARCH_DIRECTOR", "interrupted")
    result = invoke(role_workspace, "GLOBAL_RESEARCH_DIRECTOR", "continued")
    assert result.transport_status == "SUCCESS"
    assert calls[1][0][2] == "resume"
    assert calls[0][2] == calls[1][2]


def test_web_reporter_keeps_owner_home_with_its_evidence_workspace(role_workspace, monkeypatch):
    calls = native_process(monkeypatch)
    evidence = RunWorkspace.create(role_workspace.root / ".web-presentation", "evidence-copy")
    home = role_home(role_workspace.root, "WEB_REPORTER")
    handle = begin_artifact_call(CodexLLM(max_retries=0), "Explain this evidence.",
        workdir=evidence.root, call_name="web-explanation", role="web_reporter", stage="WEB_PRESENTATION",
        input_refs=(), metadata={"agent_home": str(home)})
    assert handle.transport_status == "SUCCESS"
    assert calls[0][1] == home
    assert calls[0][3] == evidence.root
    assert "--ephemeral" not in calls[0][0]


def test_rci_keeps_its_ephemeral_environment(role_workspace, monkeypatch):
    calls = native_process(monkeypatch)
    result = invoke(role_workspace, "SMI_FRONTIER_EXPLORER", "rci-phase")
    assert result.transport_status == "SUCCESS"
    cmd, home, _, _ = calls[0]
    assert "--ephemeral" in cmd and "project_doc_max_bytes=0" in cmd
    assert not home.exists()
    assert role_home(role_workspace.root, "SMI_FRONTIER_EXPLORER") is None
