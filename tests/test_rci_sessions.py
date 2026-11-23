"""RCI roles, exact phase recovery and continued questions on the production path."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pace import cli
from pace.agents.rci.phase_runner import RciPhaseError, RciPhaseRunner
from pace.agents.rci.prompt_composer import build_instructions
from pace.agents.rci.roles import ROLE_BINDINGS
from pace.artifacts.events import read_events
from pace.artifacts.workspace import RunWorkspace, work_ref
from pace.runtime import prompts
from pace.runtime.agent_environment import prepare_role_environment, role_home
from pace.runtime.chat_api import ChatCompletionsLLM, WorkspaceTools
from pace.runtime.llm import CodexLLM
from test_research_round_limit import RoundLimitTransport

REVIEW = (
    "# Original review\nThe precise question remains open; this is search context.\n"
)


@pytest.fixture
def phase_task(tmp_path, monkeypatch):
    monkeypatch.setenv("PACE_CODEX_ENV_ROOT", str(tmp_path / "environments"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "credentials"))
    ws = RunWorkspace.create(tmp_path / "runs", "rci")
    prompts.materialize_run_resources(ws.root, resource_variant="standard")
    target = ws.write_text(
        ws.work_ref("input/problem.md"), "Investigate this exact local question."
    )
    seed = ws.write_text(
        "seed.md", "# Precise candidate\nThe original question remains fixed."
    )
    directory = ws.path(
        ws.work_ref("round1/rci/attempt_001/candidate_01/raw_seed_critic_research")
    )
    task = dict(
        phase="RAW_SEED_CRITIC_RESEARCH",
        workdir=ws.root,
        call_dir=directory,
        target_contract_id="TARGET",
        round_index=1,
        input_refs={
            "TARGET_ANCHOR": str(target.relative_to(ws.root)),
            "SEED": str(seed.relative_to(ws.root)),
        },
        prompt_seed="Review only research value and readiness.",
    )
    return ws, directory, task


class NativePhases:
    def __init__(
        self,
        monkeypatch,
        *,
        interrupt=(),
        interrupt_before_thread=(),
        invalid_first=False,
    ):
        self.calls = []
        self.interrupt = set(interrupt)
        self.invalid_first = invalid_first
        self.interrupt_before_thread = set(interrupt_before_thread)
        monkeypatch.setattr("pace.runtime.llm._run_cli_process", self.run)

    def run(self, cmd, *, cwd, env, input, stdout, stderr, timeout):
        root = Path(cwd)
        response = Path(cmd[cmd.index("-o") + 1])
        record_path = response.parent.parent.parent / "record.json"
        record = json.loads(record_path.read_text())
        index = len(self.calls)
        thread = cmd[3] if cmd[2] == "resume" else f"rci-thread-{index}"
        prompt = (root / record["prompt_ref"]).read_text()
        self.calls.append(
            dict(
                cmd=cmd,
                home=Path(env["CODEX_HOME"]),
                record=record,
                ref=str(record_path.relative_to(root)),
                thread=thread,
                prompt=prompt,
            )
        )
        if index in self.interrupt_before_thread:
            raise KeyboardInterrupt
        stdout.write(json.dumps({"type": "thread.started", "thread_id": thread}) + "\n")
        stdout.flush()
        assert (Path(env["CODEX_HOME"]) / "AGENTS.md").is_file()
        assert "--ephemeral" not in cmd
        if record["source_prompt_ref"]:
            directory = (root / record["source_prompt_ref"]).parent
            if record["role"] == "rci_frontier_explorer":
                (directory / "change.md").write_text(
                    f"# Actual attempt {index}\nUNRESOLVED_GAP_{index}: a local estimate remains open.\n"
                )
            elif record["role"] == "rci_seed_scout":
                (directory / "seed.md").write_text(
                    "# Exact local question\nProve the original integer equality."
                )
            elif not (directory / "review.md").exists():
                (directory / "review.md").write_text(REVIEW)
        if index in self.interrupt:
            raise KeyboardInterrupt
        if record["role"] == "rci_frontier_explorer":
            inputs = json.loads((directory / "prompt_assembly.json").read_text())[
                "phase_inputs"
            ]["input_refs"]
            payload = {
                "action": "HANDOFF",
                "candidate_ref": inputs["REVIEWED_SEED"][0],
                "dependency_refs": [],
            }
        elif record["role"] == "rci_seed_scout":
            payload = {"dependency_refs": []}
        else:
            kind = (
                "MATHEMATICAL"
                if record["role"] == "rci_raw_seed_critic_math"
                else "RESEARCH"
            )
            payload = {"action": "ACCEPT", "review_kind": kind, "dependency_refs": []}
            if self.invalid_first and index == 0:
                payload["action"] = "INVALID"
        response.write_text(json.dumps(payload))
        stdout.write(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            )
            + "\n"
        )
        return subprocess.CompletedProcess(cmd, 0)


@pytest.mark.parametrize("variant", ["standard", "weak"])
def test_all_phase_homes_use_frozen_role_resources(tmp_path, monkeypatch, variant):
    packaged = tmp_path / "packaged"
    shutil.copytree(prompts._PACKAGED_RESOURCES, packaged)
    source = packaged / "prompts/rci/frontier_explorer/role.md"
    source.write_text(source.read_text() + "\nORIGINAL_RCI_RULE\n")
    monkeypatch.setattr(prompts, "_PACKAGED_RESOURCES", packaged)
    monkeypatch.setenv("PACE_CODEX_ENV_ROOT", str(tmp_path / "homes"))
    ws = RunWorkspace.create(tmp_path / "runs", "roles")
    prompts.materialize_run_resources(ws.root, resource_variant=variant)
    homes = set()
    for phase, binding in ROLE_BINDINGS.items():
        instructions = build_instructions(phase=phase, workdir=ws.root)
        prepare_role_environment(ws.root, binding.role, instructions=instructions)
        home = role_home(ws.root, binding.role)
        homes.add(home)
        assert (home / "AGENTS.md").read_text() == instructions
        assert "{{" not in instructions
    assert len(homes) == len(ROLE_BINDINGS) == 13
    math = (role_home(ws.root, "RCI_RAW_SEED_CRITIC_MATH") / "AGENTS.md").read_text()
    research = (
        role_home(ws.root, "RCI_RAW_SEED_CRITIC_RESEARCH") / "AGENTS.md"
    ).read_text()
    assert "Perform only the `MATHEMATICAL` review" in math
    assert "Perform only the `RESEARCH` review" in research
    first_home = role_home(ws.root, "RCI_FRONTIER_EXPLORER")
    source.write_text(source.read_text().replace("ORIGINAL_RCI_RULE", "NEW_RCI_RULE"))
    assert "ORIGINAL_RCI_RULE" in build_instructions(
        phase="FRONTIER_EXPLORER", workdir=ws.root
    )
    ws.start_turn()
    prompts.materialize_run_resources(ws.root, resource_variant=variant)
    assert "NEW_RCI_RULE" in build_instructions(
        phase="FRONTIER_EXPLORER", workdir=ws.root
    )
    assert role_home(ws.root, "RCI_FRONTIER_EXPLORER") != first_home


def test_native_phase_recovery_preserves_work_and_starts_new_phase_sessions(
    phase_task, monkeypatch
):
    ws, directory, task = phase_task
    native = NativePhases(monkeypatch, interrupt={0})
    runner = RciPhaseRunner(llm=CodexLLM(max_retries=0))
    with pytest.raises(KeyboardInterrupt):
        runner.run(**task)
    original = (directory / "review.md").read_bytes()
    result = runner.run(**task)
    assert runner.run(**task) == result
    assert len(native.calls) == 2
    assert native.calls[1]["thread"] == native.calls[0]["thread"]
    assert native.calls[1]["record"]["resume_from_call_ref"] == native.calls[0]["ref"]
    assert (directory / "review.md").read_bytes() == original
    assert not (directory / "interrupted_attempts").exists()
    assert "# RCI Raw Seed Critic" not in native.calls[0]["prompt"]
    assert not (native.calls[0]["home"] / "session.json").exists()
    runner.run(**{**task, "call_dir": directory.with_name(directory.name + "_02")})
    assert native.calls[-1]["thread"] != native.calls[0]["thread"]
    assert native.calls[-1]["record"]["resume_from_call_ref"] == ""
    with pytest.raises(RciPhaseError, match="task changed"):
        runner.run(**{**task, "prompt_seed": "Change the mathematical review."})


def test_native_resume_keeps_known_thread_when_a_restart_stops_before_start_event(
    phase_task, monkeypatch
):
    ws, directory, task = phase_task
    native = NativePhases(monkeypatch, interrupt={0}, interrupt_before_thread={1})
    runner = RciPhaseRunner(llm=CodexLLM(max_retries=0))
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            runner.run(**task)
    runner.run(**task)
    assert len(native.calls) == 3
    assert len({c["thread"] for c in native.calls}) == 1
    assert native.calls[-1]["record"]["resume_from_call_ref"] == native.calls[1]["ref"]
    assert (directory / "review.md").read_text() == REVIEW


@pytest.mark.parametrize("resource", ["common_instructions", "method_guide"])
def test_phase_reuse_binds_fixed_rules_and_methods(phase_task, monkeypatch, resource):
    ws, directory, task = phase_task
    native = NativePhases(monkeypatch)
    runner = RciPhaseRunner(llm=CodexLLM(max_retries=0))
    runner.run(**task)
    original = (directory / "review.md").read_bytes()
    if resource == "common_instructions":
        changed = prompts.prompt_root(ws.root) / "rci/shared/raw_seed_critic.md"
    else:
        from pace.agents.rci.prompt_composer import skill_refs

        changed = ws.root / skill_refs(task["phase"], workdir=ws.root)[0]
    changed.write_text(changed.read_text() + "\nChanged review rule.\n")
    with pytest.raises(RciPhaseError, match="task changed"):
        runner.run(**task)
    assert len(native.calls) == 1
    assert (directory / "review.md").read_bytes() == original


def test_interrupted_native_receipt_correction_reuses_author_and_session(
    phase_task, monkeypatch
):
    ws, directory, task = phase_task
    native = NativePhases(monkeypatch, invalid_first=True, interrupt={1})
    runner = RciPhaseRunner(llm=CodexLLM(max_retries=0))
    with pytest.raises(KeyboardInterrupt):
        runner.run(**task)
    result = runner.run(**task)
    assert result.payload["action"] == "ACCEPT"
    assert len(native.calls) == 3
    assert len({call["thread"] for call in native.calls}) == 1
    assert sum(bool(call["record"]["source_prompt_ref"]) for call in native.calls) == 1
    assert native.calls[2]["record"]["resume_from_call_ref"] == native.calls[1]["ref"]
    assert (directory / "review.md").read_text() == REVIEW
    assert runner.run(**task) == result and len(native.calls) == 3


def api_tool(name, args):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": name,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


@pytest.mark.parametrize("mode", ["execution", "correction", "interrupted_correction"])
def test_api_phase_resumes_exact_conversation_without_rewriting_math(
    phase_task, monkeypatch, mode
):
    ws, directory, task = phase_task
    correction = mode != "execution"
    llm = ChatCompletionsLLM(model="fixture", web_search=False)
    requests = []
    writes = []
    execute = WorkspaceTools.execute

    def execute_tool(self, name, args):
        if name == "write_file":
            writes.append(args["path"])
        return execute(self, name, args)

    def reply(messages, tools):
        requests.append(json.loads(json.dumps(messages)))
        if len(requests) == 1:
            return api_tool(
                "write_file",
                {
                    "path": str(directory.relative_to(ws.root) / "review.md"),
                    "content": REVIEW,
                },
            )
        if len(requests) == 2:
            if not correction:
                raise KeyboardInterrupt
            return api_tool(
                "submit_result", {"action": "INVALID", "dependency_refs": []}
            )
        if mode == "interrupted_correction" and len(requests) == 3:
            raise KeyboardInterrupt
        assert any(
            m.get("tool_calls", [{}])[0].get("function", {}).get("name") == "write_file"
            for m in messages
            if m.get("tool_calls")
        )
        if correction:
            assert (
                messages[-1]["role"] == "user"
                and "Repair only the JSON" in messages[-1]["content"]
            )
            assert messages[-2]["role"] == "tool"
            assert "RCI_RESEARCH" not in messages[0]["content"]
        return api_tool(
            "submit_result",
            {"action": "ACCEPT", "review_kind": "RESEARCH", "dependency_refs": []},
        )

    monkeypatch.setattr(llm, "_request", reply)
    monkeypatch.setattr(WorkspaceTools, "execute", execute_tool)
    runner = RciPhaseRunner(llm=llm)
    if mode in {"execution", "interrupted_correction"}:
        with pytest.raises(KeyboardInterrupt):
            runner.run(**task)
    result = runner.run(**task)
    assert result.payload["action"] == "ACCEPT"
    expected = 4 if mode == "interrupted_correction" else 3
    assert len(writes) == 1 and len(requests) == expected
    assert (directory / "review.md").read_text() == REVIEW
    assert sum(
        e["event_kind"] == "CHAT_CONVERSATION_RESUMED" for e in read_events(ws.root)
    ) == (2 if mode == "interrupted_correction" else 1)
    assert runner.run(**task) == result and len(requests) == expected


class ContinuedRciTransport(RoundLimitTransport):
    def __init__(self, *, new_problem=False):
        super().__init__("rci")
        self.new_problem = new_problem
        self.native = CodexLLM(max_retries=0)

    def complete_structured_result(
        self, prompt, *, workdir, call_name, ledger_context, **kwargs
    ):
        root = Path(workdir)
        if ledger_context["role"].startswith("rci_"):
            return self.native.complete_structured_result(
                prompt,
                workdir=root,
                call_name=call_name,
                ledger_context=ledger_context,
                **kwargs,
            )
        if call_name.startswith("global_research_director_"):
            self.round_prompts.append(prompt)
            refs = [work_ref(root, "input/problem.md")]
            operation = (
                "NEW_PROBLEM"
                if self.new_problem and len(self.round_prompts) == 1
                else "CONTINUE_PROBLEM"
            )
            if self.new_problem and len(self.round_prompts) > 1:
                refs = [
                    str(
                        next(
                            root.glob(work_ref(root, "round1/rci/**/seed.md"))
                        ).relative_to(root)
                    )
                ]
            refs.extend(
                str(p.relative_to(root))
                for p in root.glob(work_ref(root, "round*/rci/**/change.md"))
            )
            payload = {
                "action": "DISPATCH",
                "agent_id": "rci",
                "next_step_kind": "CONTINUE",
                "prompt_brief": "Continue the exact question; try a different estimate for the recorded gap.",
                "input_refs": refs,
                "archive_refs": [],
                "change_refs": [],
                "obligation_ids": ["target"],
                "branch_id": "b-target",
                "research_operation": operation,
                "research_problem_ref": refs[0]
                if operation == "CONTINUE_PROBLEM"
                else "",
            }
            return self._reply(
                json.dumps(payload),
                workdir=root,
                call_name=call_name,
                ledger_context=ledger_context,
                structured=True,
            )
        return super().complete_structured_result(
            prompt,
            workdir=root,
            call_name=call_name,
            ledger_context=ledger_context,
            **kwargs,
        )


@pytest.mark.parametrize("new_problem", [False, True])
def test_cli_continued_question_keeps_original_work_but_uses_new_round_sessions(
    tmp_path, monkeypatch, new_problem
):
    native = NativePhases(monkeypatch)
    transport = ContinuedRciTransport(new_problem=new_problem)
    monkeypatch.setenv("PACE_CODEX_ENV_ROOT", str(tmp_path / "environments"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "credentials"))
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    assert (
        cli.main(
            [
                "run",
                "--problem",
                str(problem),
                "--problem-id",
                "continued",
                "--output",
                str(tmp_path),
                "--max-rounds",
                "2",
                "--json",
            ]
        )
        == 0
    )
    root = tmp_path / "continued"
    count = 5 if new_problem else 2
    assert len(native.calls) == count
    assert len({c["thread"] for c in native.calls}) == count
    explorers = [
        c for c in native.calls if c["record"]["role"] == "rci_frontier_explorer"
    ]
    assert len(explorers) == 2
    if new_problem:
        critics = [
            c
            for c in native.calls
            if c["record"]["role"].startswith("rci_raw_seed_critic_")
        ]
        assert len(critics) == 2 and critics[0]["home"] != critics[1]["home"]
    assert all(c["record"]["resume_from_call_ref"] == "" for c in native.calls)
    second = explorers[1]["record"]
    assert second["round_index"] == 2
    assert any(
        f"UNRESOLVED_GAP_{native.calls.index(explorers[0])}" in (root / ref).read_text()
        for ref in second["input_refs"]
        if (root / ref).is_file()
    )
    final = json.loads((root / work_ref(root, "final_report.json")).read_text())
    assert final["mathematical_status"] == "OPEN"
    assert final["director_state"]["round_index"] == 2
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert len(native.calls) == count
