"""Host-owned protocol for one dynamic Research Director decision.

The Director is a search coordinator.  This module contains only the small
machine-owned invariants needed to turn its structured response into a Host
dispatch or a search stop.  It deliberately does not inspect mathematical
claims, select a proof verdict, or execute a specialist.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.runtime.schema import STRING, STRING_LIST, closed_object

DIRECTOR_PROTOCOL_SCHEMA_VERSION = "research_director_decision/v2"
DIRECTOR_ACTIONS = frozenset({"DISPATCH", "STOP"})
DISPATCHABLE_AGENT_IDS = frozenset(
    {"framer", "planner", "survey", "rci", "research", "prover"}
)
DIRECTOR_REQUIRED_FIELDS = frozenset({"action"})
DIRECTOR_FIELDS = DIRECTOR_REQUIRED_FIELDS | {
    "agent_id", "prompt_brief", "input_refs", "rationale",
    "parallel_tasks",
    "research_operation",
    "research_problem_ref",
    "archive_refs", "route_assessment", "route_reason", "route_evidence_refs",
}


class DirectorAction(str, Enum):
    """The only scheduling choices owned by the Director."""

    DISPATCH = "DISPATCH"
    STOP = "STOP"


@dataclass(frozen=True, slots=True)
class SpecialistSpec:
    """Static Host metadata for a dispatchable specialist.

    The registry is intentionally declarative.  The Host binds an entry to
    the concrete specialist implementation and owns execution of that
    implementation.
    """

    agent_id: str
    role: str

    def __post_init__(self) -> None:
        if self.agent_id not in DISPATCHABLE_AGENT_IDS:
            raise ValueError(f"unknown dispatchable specialist: {self.agent_id}")
        if not self.role.strip():
            raise ValueError("specialist metadata requires role")


# Auditor and Kernel are intentionally absent: they are fixed verification
# boundaries and cannot be scheduled as ordinary research specialists.
SPECIALIST_REGISTRY: Mapping[str, SpecialistSpec] = {
    "framer": SpecialistSpec("framer", "DIRECTOR_LED_FRAMER"),
    "planner": SpecialistSpec("planner", "PLANNER"),
    "survey": SpecialistSpec("survey", "SURVEY"),
    "rci": SpecialistSpec("rci", "RCI"),
    "research": SpecialistSpec("research", "RESEARCH"),
    "prover": SpecialistSpec("prover", "WHOLE_PROVER"),
}


@dataclass(frozen=True, slots=True)
class DirectorDecision:
    """A validated, Host-actionable Director response."""

    action: DirectorAction
    agent_id: str
    prompt_brief: str
    input_refs: tuple[str, ...]
    rationale: str
    parallel_tasks: tuple[DirectorDecision, ...] = ()
    research_operation: str = ""
    research_problem_ref: str = ""
    archive_refs: tuple[str, ...] = ()
    route_assessment: str = ""
    route_reason: str = ""
    route_evidence_refs: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "agent_id": self.agent_id,
            "prompt_brief": self.prompt_brief,
            "input_refs": list(self.input_refs),
            "rationale": self.rationale,
            "research_operation": self.research_operation,
            "research_problem_ref": self.research_problem_ref,
            "parallel_tasks": [
                {k: v for k, v in task.to_json().items() if k != "parallel_tasks"}
                for task in self.parallel_tasks
            ],
            **({"archive_refs": list(self.archive_refs)} if self.archive_refs else {}),
            **({"route_assessment": self.route_assessment, "route_reason": self.route_reason,
                "route_evidence_refs": list(self.route_evidence_refs)} if self.route_assessment else {}),
        }


class DirectorProtocolError(ValueError):
    """Raised when a Director response violates the Host protocol."""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


def director_decision_schema(*, survey_calls_remaining: int = 4) -> dict[str, Any]:
    """Describe scheduling fields; input paths are reading suggestions."""

    task_fields = {
        "action": {**STRING, "enum": ["DISPATCH"]},
        "agent_id": {
            **STRING,
            "enum": sorted(
                DISPATCHABLE_AGENT_IDS
                - ({"survey"} if survey_calls_remaining < 2 else set())
            ),
        },
        "prompt_brief": STRING,
        "input_refs": STRING_LIST,
        "archive_refs": STRING_LIST,
        "rationale": STRING,
        "research_operation": {
            **STRING,
            "enum": ["", "NEW_PROBLEM", "CONTINUE_PROBLEM"],
        },
        "research_problem_ref": {
            **STRING,
            "description": "Exact existing run-relative question file from input_refs, without prose or a Markdown link; empty when unused.",
        },
    }
    return closed_object(
        {
            "action": {**STRING, "enum": sorted(DIRECTOR_ACTIONS)},
            "agent_id": {
                **STRING,
                "enum": [
                    "",
                    *sorted(
                        DISPATCHABLE_AGENT_IDS
                        - ({"survey"} if survey_calls_remaining == 0 else set())
                    ),
                ],
            },
            "prompt_brief": STRING,
            "input_refs": STRING_LIST,
            "archive_refs": STRING_LIST,
            "route_assessment": {**STRING, "enum": ["KEEP", "REFRESH"]},
            "route_reason": STRING,
            "route_evidence_refs": STRING_LIST,
            "rationale": STRING,
            "research_operation": {
                **STRING,
                "enum": ["", "NEW_PROBLEM", "CONTINUE_PROBLEM"],
            },
            "research_problem_ref": task_fields["research_problem_ref"],
            "parallel_tasks": {
                "type": "array",
                "maxItems": 1,
                "items": closed_object(task_fields, required=("action", "agent_id", "prompt_brief")),
            },
        },
        required=("action",),
    )


def dispatchable_agent_ids() -> tuple[str, ...]:
    """Return deterministic registry order for Host dispatch validation."""

    return tuple(SPECIALIST_REGISTRY)


def specialist_spec(agent_id: str) -> SpecialistSpec:
    """Resolve one dispatchable specialist or raise a protocol error."""

    key = str(agent_id or "")
    try:
        return SPECIALIST_REGISTRY[key]
    except KeyError as exc:
        raise DirectorProtocolError(
            f"DISPATCH agent_id must be one of {sorted(DISPATCHABLE_AGENT_IDS)}",
            field="agent_id",
        ) from exc


def parse_director_decision(payload: Mapping[str, Any]) -> DirectorDecision:
    """Parse one scheduling response; agents choose mathematical guidance."""

    if not isinstance(payload, Mapping):
        raise DirectorProtocolError("director decision must be a JSON object")
    if not DIRECTOR_REQUIRED_FIELDS.issubset(payload) or set(payload) - DIRECTOR_FIELDS:
        missing = sorted(DIRECTOR_REQUIRED_FIELDS.difference(payload))
        extra = sorted(set(payload).difference(DIRECTOR_FIELDS))
        details = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected fields: {extra}")
        raise DirectorProtocolError(
            "director decision fields are closed"
            + (" (" + "; ".join(details) + ")" if details else "")
        )

    action_value = payload.get("action")
    if not isinstance(action_value, str) or action_value not in DIRECTOR_ACTIONS:
        raise DirectorProtocolError("action must be DISPATCH or STOP", field="action")
    action = DirectorAction(action_value)

    agent_id = _text(payload.get("agent_id", ""), "agent_id")
    prompt_brief = _text(payload.get("prompt_brief", ""), "prompt_brief")
    rationale = _text(payload.get("rationale", ""), "rationale")
    input_refs = _string_tuple(payload.get("input_refs", []), "input_refs")
    companions = payload.get("parallel_tasks", [])
    if not isinstance(companions, list) or len(companions) > 1:
        raise DirectorProtocolError(
            "parallel_tasks permits at most one independent companion"
        )
    tasks = []
    for index, task in enumerate(companions):
        if (
            not isinstance(task, Mapping)
            or not DIRECTOR_REQUIRED_FIELDS <= set(task)
            or set(task) - DIRECTOR_FIELDS
            or "parallel_tasks" in task
        ):
            raise DirectorProtocolError(
                "parallel task must be a single dispatch, without nested tasks"
            )
        try:
            parsed = parse_director_decision(task)
        except DirectorProtocolError as exc:
            raise DirectorProtocolError(str(exc), field=f"parallel_tasks/{index}/{exc.field}") from exc
        if parsed.action is not DirectorAction.DISPATCH:
            raise DirectorProtocolError("parallel task must DISPATCH")
        tasks.append(parsed)

    decision = DirectorDecision(
        action=action,
        agent_id=agent_id,
        prompt_brief=prompt_brief,
        input_refs=input_refs,
        rationale=rationale,
        parallel_tasks=tuple(tasks),
        research_operation=_text(
            payload.get("research_operation", ""), "research_operation"
        ),
        research_problem_ref=_text(
            payload.get("research_problem_ref", ""), "research_problem_ref"
        ),
        archive_refs=_string_tuple(payload.get("archive_refs", []), "archive_refs"),
        route_assessment=_text(payload.get("route_assessment", ""), "route_assessment"),
        route_reason=_text(payload.get("route_reason", ""), "route_reason"),
        route_evidence_refs=_string_tuple(payload.get("route_evidence_refs", []), "route_evidence_refs"),
    )
    validate_director_decision(decision)
    return decision


def validate_director_decision(decision: DirectorDecision) -> None:
    """Check only Host-owned scheduling invariants."""

    if not isinstance(decision, DirectorDecision):
        raise DirectorProtocolError("decision must be a DirectorDecision")
    if decision.route_assessment not in {"", "KEEP", "REFRESH"}:
        raise DirectorProtocolError("route_assessment must be KEEP or REFRESH", field="route_assessment")
    if decision.route_assessment and not decision.route_reason.strip():
        raise DirectorProtocolError("explain the route assessment", field="route_reason")
    if decision.route_assessment == "REFRESH" and (
        decision.action is not DirectorAction.DISPATCH or decision.agent_id != "planner"
        or decision.parallel_tasks or not decision.route_evidence_refs
    ):
        raise DirectorProtocolError("a substantive route milestone must dispatch Planner with evidence before downstream work", field="route_assessment")
    if decision.action is DirectorAction.DISPATCH:
        specialist_spec(decision.agent_id)
    if decision.agent_id == "rci" and decision.action is DirectorAction.DISPATCH:
        if decision.research_operation not in {"NEW_PROBLEM", "CONTINUE_PROBLEM"}:
            raise DirectorProtocolError("RCI requires an explicit research operation", field="research_operation")
        if (
            decision.research_operation == "CONTINUE_PROBLEM"
            and not decision.research_problem_ref
        ):
            raise DirectorProtocolError("continued RCI requires research_problem_ref", field="research_problem_ref")
    elif decision.research_operation or decision.research_problem_ref:
        raise DirectorProtocolError(
            "research operation fields are only for RCI; "
            "omit or clear both research_operation and research_problem_ref for other agents",
            field="research_operation" if decision.research_operation else "research_problem_ref",
        )
    if decision.action is DirectorAction.STOP:
        for field in ("agent_id", "prompt_brief", "input_refs", "archive_refs", "parallel_tasks"):
            if getattr(decision, field):
                raise DirectorProtocolError(
                    "STOP must not dispatch an agent or select inputs", field=field
                )
        return

    if decision.action is not DirectorAction.DISPATCH:
        raise DirectorProtocolError("unsupported Director action", field="action")
    if len(decision.parallel_tasks) > 1:
        raise DirectorProtocolError("at most two tasks may run concurrently")
    for task in decision.parallel_tasks:
        if task.action is not DirectorAction.DISPATCH or task.parallel_tasks:
            raise DirectorProtocolError("parallel tasks must be non-nested dispatches")
        validate_director_decision(task)
    if not decision.prompt_brief.strip():
        raise DirectorProtocolError(
            "DISPATCH requires a non-empty prompt_brief", field="prompt_brief"
        )


def validate_director_reference_bindings(decision: DirectorDecision, workdir: Path) -> None:
    """Require continuation pointers to be declared, existing workspace files.

    Reading suggestions are advisory; the continuation pointer is executable
    input and must be checked before committing a dispatch.
    """
    from pace.runtime.output.workspace import is_call_path, path_has_symlink_component

    root = Path(workdir).resolve()
    for index, task in enumerate((decision, *decision.parallel_tasks)):
        for field in ("archive_refs", "route_evidence_refs"):
            for extra in getattr(task, field):
                file = Path(extra)
                if (file.is_absolute() or ".." in file.parts or "\\" in extra or PureWindowsPath(extra).drive
                        or is_call_path(file) or file.name.startswith("private_")
                        or path_has_symlink_component(root, root / file)
                        or not ((root / file).is_file() or field == "archive_refs" and (root / file).is_dir())):
                    pointer = field if index == 0 else f"parallel_tasks/{index - 1}/{field}"
                    raise DirectorProtocolError("archive refs must name existing run-local files or directories; milestone evidence must name files", field=pointer)
        ref = task.research_problem_ref
        if not ref:
            continue
        path = Path(ref)
        field = "research_problem_ref" if index == 0 else f"parallel_tasks/{index - 1}/research_problem_ref"
        if (ref not in task.input_refs or ref != ref.strip() or path.is_absolute()
                or PureWindowsPath(ref).drive or "\\" in ref
                or any(part in {".", ".."} for part in ref.split("/"))
                or is_call_path(path) or path.name.startswith("private_")):
            raise DirectorProtocolError(
                "research_problem_ref must be an exact run-relative question file "
                "listed in input_refs; put question descriptions in prompt_brief",
                field=field,
            )
        candidate = root / path
        if (path_has_symlink_component(root, candidate)
                or not candidate.resolve().is_relative_to(root)
                or not candidate.is_file()):
            raise DirectorProtocolError(
                f"research_problem_ref does not name an existing workspace file: {ref}",
                field=field,
            )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DirectorProtocolError(f"{field} must be a string", field=field)
    return value


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DirectorProtocolError(
            f"{field} must be a list of strings", field=field
        )
    if any(not item.strip() for item in value):
        raise DirectorProtocolError(
            f"{field} entries must be non-empty strings", field=field
        )
    # Reading suggestions preserve literal paths and first occurrence.
    return tuple(dict.fromkeys(value))


__all__ = [
    "DIRECTOR_ACTIONS",
    "DIRECTOR_FIELDS",
    "DIRECTOR_PROTOCOL_SCHEMA_VERSION",
    "DISPATCHABLE_AGENT_IDS",
    "SPECIALIST_REGISTRY",
    "DirectorAction",
    "DirectorDecision",
    "DirectorProtocolError",
    "SpecialistSpec",
    "director_decision_schema",
    "dispatchable_agent_ids",
    "parse_director_decision",
    "specialist_spec",
    "validate_director_decision",
    "validate_director_reference_bindings",
]
