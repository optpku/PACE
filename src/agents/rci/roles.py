"""Prompt and input bindings for the six RCI roles."""

from dataclasses import dataclass
from pathlib import Path

from pace.artifacts.workspace import work_ref

PROMPT_ROOT = "resources/prompts/rci"
SKILL_ROOT = "resources/skills/rci-investigation/references"
RCI_SKILL_CORE_REF = f"{SKILL_ROOT}/rci_search_core.md"

PHASE_ACTIONS = {
    "CONJECTURE_EXPLORER": (
        "PROPOSE_CONJECTURE",
        "EXPLORE_MECHANISM",
        "REVIEW_MECHANISM",
        "PROBE_PROOF",
        "PROBE_REFUTATION",
        "HANDOFF",
    ),
    "MECHANISM_EXPLORER": ("PROPOSE_MECHANISM", "CONSULT_CONJECTURE", "HANDOFF"),
    "CANDIDATE_CRITIC": ("ACCEPT", "REPAIR", "REJECT", "REUSE"),
}

AUTHORS = {"CONJECTURE_EXPLORER", "MECHANISM_EXPLORER"}
ANCHOR_INPUTS = ("TARGET_ANCHOR", "WORK_ITEM")


@dataclass(frozen=True)
class RciRoleBinding:
    phase: str
    skill_refs: tuple[str, ...]
    required_inputs: tuple[str, ...]
    extra_inputs: tuple[str, ...] = ()

    @property
    def role(self) -> str:
        return f"RCI_{self.phase}"

    @property
    def prompt_ref(self) -> str:
        return f"{PROMPT_ROOT}/{self.phase.lower()}/role.md"


def _binding(phase, skills, required, extra=()) -> RciRoleBinding:
    return RciRoleBinding(
        phase, tuple(f"{SKILL_ROOT}/{name}.md" for name in skills), required, extra
    )


ROLE_BINDINGS = {
    binding.phase: binding
    for binding in (
        _binding(
            "CONJECTURE_EXPLORER",
            ("raw_seed/claim_shape_contract", "raw_seed/contribution_boundary"),
            ANCHOR_INPUTS,
            (
                "CANDIDATE",
                "RELATED_CANDIDATE",
                "DRAFT_CANDIDATE",
                "MECHANISM",
                "EVIDENCE",
                "SOURCE",
            ),
        ),
        _binding(
            "MECHANISM_EXPLORER",
            ("mechanism/proof_mechanism", "mechanism/mechanism_repair"),
            (*ANCHOR_INPUTS, "CANDIDATE"),
            ("MECHANISM", "BASE_MECHANISM", "EVIDENCE", "SOURCE"),
        ),
        _binding(
            "CANDIDATE_CRITIC",
            ("raw_seed/claim_shape_contract", "raw_seed/contribution_boundary"),
            (*ANCHOR_INPUTS, "CANDIDATE"),
            ("RELATED_CANDIDATE", "SOURCE"),
        ),
        _binding(
            "MECHANISM_CRITIC",
            ("multi_mechanism/mechanism_audit", "multi_mechanism/bundle_closure_audit"),
            (*ANCHOR_INPUTS, "CANDIDATE", "MECHANISM"),
            ("PREVIOUS_MECHANISM", "RELATED_MECHANISM", "EVIDENCE", "SOURCE"),
        ),
        _binding(
            "BLIND_PROVER_PROBE",
            ("raw_seed/attack_surface_failure_tests",),
            ("PROBE_TASK",),
        ),
        _binding(
            "REFUTE_PROVER_PROBE",
            ("critic/raw_seed/attack_counterexamples",),
            ("PROBE_TASK",),
        ),
    )
}


def resolve_skill_refs(workdir: Path, refs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(work_ref(workdir, ref) for ref in dict.fromkeys(refs))


def role_binding(phase: str) -> RciRoleBinding:
    return ROLE_BINDINGS[phase]
