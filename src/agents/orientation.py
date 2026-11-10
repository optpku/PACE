"""Locate the literal target and ask Orientation to write its own research brief."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from pace.contracts.problem import Problem
from pace.contracts.target import (
    SCHEMA_VERSION as TARGET_CONTRACT_SCHEMA_VERSION,
)
from pace.contracts.target import target_contract_id
from pace.runtime.json_values import dict_rows as _rows
from pace.runtime.output.workspace import RunWorkspace, work_ref
from pace.runtime.prompts import render_prompt_template
from pace.agents.roles import SemanticRoleSpec
from pace.runtime.schema import (
    INTEGER,
    STRING,
    array_of,
    closed_object,
    open_object,
    validate_json_instance,
)
from pace.runtime.semantic import (
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    interface_failure_payload,
    representation_failure_payload,
    semantic_call,
)


def run_problem_orientation(
    *, llm: Any, workdir: Path, problem: Problem,
    requested_mode: str | None = None, on_mode_selected=None,
) -> dict[str, Any]:
    """Persist one literal target and deliver references to role-authored context."""

    root = Path(workdir)
    workspace = RunWorkspace(root=root, problem_id=problem.problem_id)
    text = _normalize_newlines(problem.text).rstrip("\n") + "\n"
    checkpoint_ref = work_ref(root, "state/orientation_state.json")
    checkpoint_path = root / checkpoint_ref
    from pace.runtime.output.workspace import path_has_symlink_component
    if path_has_symlink_component(root, checkpoint_path):
        raise ValueError("orientation checkpoint must not contain a symlink")
    state = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else {
        "schema_version": "orientation_state/v2",
        "problem_text": text, "phase": "EXTRACTION", "requested_mode": requested_mode,
    }
    if (state.get("schema_version") != "orientation_state/v2" or state.get("problem_text") != text
            or state.get("phase") not in {"EXTRACTION", "TARGET", "ASSESSMENT", "ASSESSMENT_RESULT", "READY"}):
        raise ValueError("orientation checkpoint differs from the literal input")

    def save() -> None:
        workspace.write_json(checkpoint_ref, state)

    def committed_json(ref: str, payload: dict[str, Any]) -> None:
        path = root / ref
        if path_has_symlink_component(root, path):
            raise ValueError(f"committed initialization file must not contain a symlink: {ref}")
        if path.is_file():
            if json.loads(path.read_text(encoding="utf-8")) != payload:
                raise ValueError(f"committed initialization file changed: {ref}")
        else:
            workspace.write_json(ref, payload)

    recommend = state["requested_mode"] is None
    extraction_role = replace(
        _PROBLEM_EXTRACTION_ROLE,
        schema=lambda: problem_extraction_output_schema(recommend_mode=recommend),
        host_schema_version="target_locations/v2",
    )
    def accept_extraction(payload):
        state.update(phase="TARGET", extraction=payload)
        save()

    extraction_role = replace(extraction_role, on_accept=accept_extraction)
    save()
    if state["phase"] == "EXTRACTION":
        extraction = semantic_call(
            llm, build_problem_extraction_prompt(text, workdir=root, recommend_mode=recommend),
            workdir=root, call_name="problem_orientation_extract", input_refs=(),
            spec=extraction_role.call_spec(
                validator=lambda payload: validate_problem_extraction_payload(text, payload),
            ),
            host_metadata={"task_id": "orientation", "stage_ref": work_ref(root, "stages/orientation")},
        )
        if isinstance(extraction, SemanticInterfaceFailure):
            return {"stage_status": "INTERFACE_FAILED", "interface_failure":
                    interface_failure_payload(extraction, stage="PROBLEM_ORIENTATION_EXTRACTION")}
        if isinstance(extraction, SemanticRepresentationFailure):
            return {"stage_status": "SEMANTIC_FAILED", "semantic_failure":
                    representation_failure_payload(extraction, stage="PROBLEM_ORIENTATION_EXTRACTION")}
    errors = validate_problem_extraction_payload(text, state["extraction"])
    if errors:
        raise ValueError("saved target extraction is invalid")
    locations_ref = work_ref(root, "stages/orientation/target_locations.json")
    target_ref = work_ref(root, "input/target_contract.json")
    if recommend:
        if validate_json_instance(state["extraction"], problem_extraction_output_schema(recommend_mode=True)):
            raise ValueError("saved mode recommendation is invalid")
    committed_json(locations_ref, {"target_clauses": state["extraction"]["target_clauses"]})
    committed_json(target_ref, _compile_target_contract(problem, text, state["extraction"]))
    if state["phase"] == "TARGET":
        state.update(
            mode=state["requested_mode"] or state["extraction"]["recommended_mode"],
            mode_source="CLI" if state["requested_mode"] else "ORIENTATION",
            mode_reason="Explicit user selection" if state["requested_mode"] else state["extraction"]["mode_reason"],
        )
        state["phase"] = "ASSESSMENT"
        save()

    if state.get("mode") not in {"proof", "research"}:
        raise ValueError("saved initialization has no resolved mode")
    if on_mode_selected is not None:
        on_mode_selected(state["mode"], state["mode_source"], state["mode_reason"])
    proof_brief = state["mode"] == "proof"
    assessment_ref = work_ref(root, "stages/orientation/assessment.json")
    brief_ref = work_ref(root, "input/problem_brief.md")
    def accept_brief(payload):
        state["brief" if proof_brief else "assessment"] = payload
        state.update(phase="ASSESSMENT_RESULT", diagnostic={})
        save()

    brief_role = replace(_PROOF_BRIEF_ROLE if proof_brief else _RESEARCH_DECISION_ROLE,
                         on_accept=accept_brief)
    if state["phase"] == "ASSESSMENT":
        assessment = semantic_call(
            llm, build_research_decision_prompt(workdir=root, proof_brief=proof_brief), workdir=root,
            call_name="problem_research_decision",
            spec=brief_role.call_spec(
                validator=(lambda payload: [] if payload.get("brief_ref") == brief_ref
                           and (root / brief_ref).is_file() and (root / brief_ref).read_text().strip()
                           else [{"json_pointer": "/brief_ref", "expected": "the nonempty requested brief file"}])
                           if proof_brief else None,
            ),
            input_refs=(work_ref(root, "input/problem.md"), target_ref),
            host_metadata={"output_ref": brief_ref if proof_brief else assessment_ref,
                           "task_id": "orientation", "stage_ref": work_ref(root, "stages/orientation")},
        )
        diagnostic = {}
        if isinstance(assessment, SemanticInterfaceFailure):
            # A service/transport failure pauses the owning call. Only an
            # unusable optional assessment payload may use the default budget.
            return {"stage_status": "INTERFACE_FAILED", "interface_failure":
                    interface_failure_payload(assessment, stage="PROBLEM_ORIENTATION_BRIEF")}
        elif isinstance(assessment, SemanticRepresentationFailure):
            if proof_brief:
                return {"stage_status": "SEMANTIC_FAILED", "semantic_failure":
                        representation_failure_payload(assessment, stage="PROBLEM_ORIENTATION_BRIEF")}
            diagnostic = representation_failure_payload(assessment, stage="PROBLEM_ORIENTATION_BRIEF")
        # Invalid optional assessment content keeps the default budget fallback.
        if state["phase"] == "ASSESSMENT":
            state.update(phase="ASSESSMENT_RESULT", diagnostic=diagnostic)
            save()
    result = {"stage_status": "READY", "target_contract_ref": target_ref,
              "problem_brief_ref": "", "assessment_ref": "", "diagnostics_ref": ""}
    result.update(execution_mode=state["mode"], mode_source=state["mode_source"], mode_reason=state["mode_reason"])
    if "brief" in state:
        if not (root / brief_ref).is_file() or not (root / brief_ref).read_text().strip():
            raise ValueError("committed problem brief is missing; restore it before recovery")
        result["problem_brief_ref"] = brief_ref
    diagnostic = state.get("diagnostic", {})
    if "assessment" in state:
        if validate_json_instance(state["assessment"], closed_object({"proof_assessment": proof_assessment_schema()})):
            raise ValueError("saved proof assessment is invalid")
        committed_json(assessment_ref, state["assessment"])
        result["assessment_ref"] = assessment_ref
        if (root / brief_ref).is_file() and (root / brief_ref).read_text(encoding="utf-8").strip():
            result["problem_brief_ref"] = brief_ref
        else:
            diagnostic = {"reason_code": "orientation_brief_missing", "output_ref": brief_ref}
    if diagnostic:
        result["diagnostics_ref"] = work_ref(root, "stages/orientation/brief_diagnostics.json")
        committed_json(result["diagnostics_ref"], diagnostic)
    state["phase"] = "READY"
    save()
    return result


def build_research_decision_prompt(*, workdir: Path, proof_brief: bool = False) -> str:
    mode = "proof" if proof_brief else "research"
    prompt = f"Host execution mode: {mode}.\n\n" + render_prompt_template(
        "roles/research_decision.md",
        workdir=workdir,
        problem_ref=work_ref(workdir, "input/problem.md"),
        target_ref=work_ref(workdir, "input/target_contract.json"),
        brief_ref=work_ref(workdir, "input/problem_brief.md"),
    )

    if proof_brief:
        prompt += ("\nHost receipt contract for this proof turn: write the short problem brief and its H1 title. "
                   "Do not assess a research budget. Return only {\"brief_ref\": \"" + work_ref(workdir, "input/problem_brief.md") + "\"}.\n")
    return prompt


def proof_assessment_schema() -> dict[str, Any]:
    """The three control fields consumed by the Host's resource budget."""

    level = {**INTEGER, "enum": [1, 2, 3]}
    return closed_object({
        "difficulty": {**STRING, "enum": ["routine", "standard", "research"]},
        "plan_node_level": level,
        "dependency_depth_level": level,
    })


def build_problem_extraction_prompt(
    problem_text: str, *, workdir: Path, recommend_mode: bool = False,
) -> str:
    context = {
        "recommend_mode": recommend_mode,
        "source_lines": [
            {"line": index, "text": line}
            for index, line in enumerate(_normalize_newlines(problem_text).splitlines(), 1)
        ],
    }
    return render_prompt_template(
        "roles/problem_extraction.md", workdir=workdir,
        host_context=json.dumps(context, ensure_ascii=False, indent=2),
    )


def problem_extraction_output_schema(*, recommend_mode: bool = False) -> dict[str, Any]:
    locator = {
        "start_line": INTEGER,
        "end_line": INTEGER,
    }
    # Select locations, never ask the model to retranscribe the authority text.
    target_clause = open_object(locator)
    fields = {"target_clauses": array_of(target_clause, min_items=1)}
    if recommend_mode:
        fields.update(recommended_mode={**STRING, "enum": ["proof", "research"]},
                      mode_reason={**STRING, "minLength": 1})
    schema = open_object(fields)
    if recommend_mode:
        schema["required"] = list(fields)
    return schema


def validate_problem_extraction_payload(
    problem_text: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    lines = _normalize_newlines(problem_text).splitlines(keepends=True)
    errors: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    target_rows = _rows(payload.get("target_clauses"))
    if not target_rows:
        errors.append(
            {
                "stage": "semantic_validation",
                "json_pointer": "/target_clauses",
                "expected": "at least one literal target clause",
            }
        )
    for index, row in enumerate(target_rows):
        pointer = f"/target_clauses/{index}"
        start, end = row.get("start_line"), row.get("end_line")
        if (type(start) is not int or type(end) is not int
                or not 1 <= start <= end <= len(lines)
                or not "".join(lines[start - 1:end]).strip()):
            errors.append(
                {
                    "stage": "semantic_validation",
                    "json_pointer": pointer,
                    "expected": "a nonempty source range with 1-based inclusive integer line numbers",
                }
            )
            continue
        key = (start, end)
        if key in seen:
            errors.append(
                {
                    "stage": "semantic_validation",
                    "json_pointer": pointer,
                    "expected": "a non-duplicate target clause",
                }
            )
        seen.add(key)
    return errors


def _compile_target_contract(
    problem: Problem,
    text: str,
    extraction: dict[str, Any],
) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(extraction.get("target_clauses")), start=1):
        clause_id = f"TC{index:03d}"
        clauses.append(
            {
                "clause_id": clause_id,
                "literal_span": "".join(
                    text.splitlines(keepends=True)[row["start_line"] - 1:row["end_line"]]
                ).rstrip("\n"),
                "start_line": _int(row.get("start_line")),
                "end_line": _int(row.get("end_line")),
            }
        )
    core = {
        "problem_statement_id": str(problem.problem_id or ""),
        "required_clauses": clauses,
    }
    return {
        "schema_version": TARGET_CONTRACT_SCHEMA_VERSION,
        **core,
        "contract_id": target_contract_id(core),
    }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_newlines(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


_PROBLEM_EXTRACTION_ROLE = SemanticRoleSpec(
    role="problem_orientation_extractor",
    schema=problem_extraction_output_schema,
    host_schema_version="target_locations/v2",
    authority="ProblemContentExtractor",
)
_RESEARCH_DECISION_ROLE = SemanticRoleSpec(
    role="problem_research_decision",
    schema=lambda: closed_object({"proof_assessment": proof_assessment_schema()}),
    host_schema_version="orientation_assessment/v1",
    authority="SEARCH_CONTEXT_ONLY",
    fatal_scope="CALL",
)

_PROOF_BRIEF_ROLE = SemanticRoleSpec(
    role="problem_research_decision",
    schema=lambda: closed_object({"brief_ref": {**STRING, "minLength": 1}}),
    host_schema_version="proof_brief/v1",
    authority="SEARCH_CONTEXT_ONLY",
    fatal_scope="CALL",
)
