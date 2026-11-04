"""Static role and Skill bindings for the SMI capsule.

The bindings deliberately describe files, not mathematical payload schemas.
Prompts own the meaning of every mathematical artifact.  The Host uses these
records only to select prompt resources and identify useful predecessors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pace.runtime.output.workspace import work_ref

SMI_MODE: Final = "SMI"
PROMPT_ROOT: Final = "resources/prompts/smi"
SKILL_ROOT: Final = "resources/skills/smi-investigation/references"
SMI_SKILL_CORE_REF: Final = f"{SKILL_ROOT}/smi_search_core.md"
SMI_SKILL_ENTRY_REF: Final = "resources/skills/smi-investigation/SKILL.md"

# These menus bind Host transitions, not the contents of mathematical judgments.
PHASE_ACTIONS: Final = {
    "RAW_SEED_CRITIC_MATH": ("ACCEPT", "REPAIR", "REJECT"),
    "RAW_SEED_CRITIC_RESEARCH": ("ACCEPT", "REPAIR", "REJECT"),
    "FRONTIER_EXPLORER": (
        "PROPOSE_MECHANISM",
        "ADD_MECHANISM",
        "REQUEST_REFUTATION_PROBE",
        "REVISE_FRONTIER",
        "SPLIT",
        "EMIT",
        "HANDOFF",
        "REJECT",
        "CLAIM_RESOLVED",
    ),
    "FRONTIER_REFUTE_PROBE": (
        "COUNTEREXAMPLE_CANDIDATE",
        "NO_COUNTEREXAMPLE",
        "INVALID_FRONTIER",
    ),
    "MECHANISM_CRITIC": ("PROMOTE", "REPAIR", "REJECT"),
    "COVERAGE_JUDGE": (
        "INSUFFICIENT_SUPPORT",
        "SUBSTANTIVE_PARTIAL",
        "FULL_COVERAGE",
        "SPLIT",
        "REVISE_FRONTIER",
        "RESOLUTION_CANDIDATE",
        "REJECT",
    ),
    "FINAL_CHALLENGER": (
        "ACCEPT",
        "RUN_PROBES",
        "REPAIR",
        "SPLIT",
        "RESOLUTION_CANDIDATE",
        "REJECT",
    ),
    "FRONTIER_HARDENER": ("HARDEN", "RESOLUTION_CANDIDATE", "REJECT"),
    "BLIND_PROVER_PROBE": (
        "COMPLETE_SOLUTION",
        "PARTIAL_PROGRESS",
        "NO_FIRST_STEP",
        "INVALID_STATEMENT",
    ),
    "REFUTE_PROVER_PROBE": (
        "COUNTEREXAMPLE_CANDIDATE",
        "NO_COUNTEREXAMPLE",
        "PARTIAL_PROGRESS",
        "INVALID_STATEMENT",
    ),
    "STATEMENT_NORMALIZER": ("NORMALIZE", "REJECT"),
}


def resolve_skill_refs(workdir: Path, refs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(work_ref(workdir, ref) for ref in dict.fromkeys(refs))


@dataclass(frozen=True)
class SmiRoleBinding:
    """One model phase's immutable resources and predecessor hints."""

    phase: str
    role: str
    prompt_ref: str
    skill_refs: tuple[str, ...]
    required_inputs: tuple[str, ...]


def _skills(*paths: str) -> tuple[str, ...]:
    return tuple(f"{SKILL_ROOT}/{path}" for path in paths)


def _binding(
    phase: str,
    prompt: str,
    *,
    skills: tuple[str, ...],
    required: tuple[str, ...],
) -> SmiRoleBinding:
    return SmiRoleBinding(
        phase=phase,
        role=f"SMI_{prompt.removesuffix('.md').upper()}",
        prompt_ref=f"{PROMPT_ROOT}/{prompt}",
        skill_refs=skills,
        required_inputs=required,
    )


ROLE_BINDINGS: Final[dict[str, SmiRoleBinding]] = {
    "SEED_SCOUT": _binding(
        "SEED_SCOUT",
        "seed_scout.md",
        skills=_skills(
            "raw_seed/claim_shape_contract.md",
            "raw_seed/contribution_boundary.md",
            "raw_seed/source_gap_seed_discovery.md",
            "raw_seed/attack_surface_failure_tests.md",
        ),
        required=("TARGET_ANCHOR", "WORK_ITEM"),
    ),
    "RAW_SEED_CRITIC_MATH": _binding(
        "RAW_SEED_CRITIC_MATH",
        "raw_seed_critic.md",
        skills=_skills(
            "critic/index.md",
            "critic/raw_seed/attack_correctness.md",
            "critic/raw_seed/attack_counterexamples.md",
            "critic/raw_seed/attack_readiness_for_next_stage.md",
            "raw_seed/attack_surface_failure_tests.md",
        ),
        required=("TARGET_ANCHOR", "SEED"),
    ),
    "RAW_SEED_CRITIC_RESEARCH": _binding(
        "RAW_SEED_CRITIC_RESEARCH",
        "raw_seed_critic.md",
        skills=_skills(
            "critic/index.md",
            "critic/raw_seed/attack_novelty_value.md",
            "critic/raw_seed/attack_readiness_for_next_stage.md",
            "raw_seed/contribution_boundary.md",
            "validation/literature_policy_validation.md",
        ),
        required=("TARGET_ANCHOR", "SEED"),
    ),
    "FRONTIER_EXPLORER": _binding(
        "FRONTIER_EXPLORER",
        "frontier_explorer.md",
        skills=_skills(
            "mechanism/expert_mechanism_discovery.md",
            "mechanism/proof_mechanism.md",
            "mechanism/mechanism_repair.md",
            "multi_mechanism/joint_mechanism_interface.md",
        ),
        required=("TARGET_ANCHOR", "REVIEWED_SEED"),
    ),
    "FRONTIER_REFUTE_PROBE": _binding(
        "FRONTIER_REFUTE_PROBE",
        "frontier_refute_probe.md",
        skills=_skills("critic/raw_seed/attack_counterexamples.md"),
        required=("PROBE_TASK",),
    ),
    "MECHANISM_CRITIC": _binding(
        "MECHANISM_CRITIC",
        "mechanism_critic.md",
        skills=_skills(
            "multi_mechanism/mechanism_audit.md",
            "multi_mechanism/capability_interface_contract.md",
            "multi_mechanism/joint_mechanism_interface.md",
            "critic/raw_seed/attack_correctness.md",
        ),
        required=("TARGET_ANCHOR", "CANDIDATE", "MECHANISM"),
    ),
    "COVERAGE_JUDGE": _binding(
        "COVERAGE_JUDGE",
        "coverage_judge.md",
        skills=_skills(
            "multi_mechanism/bundle_closure_audit.md",
            "multi_mechanism/bundle_composition.md",
            "multi_mechanism/evidence_ledger.md",
            "multi_mechanism/joint_chain_frontier_audit.md",
            "multi_mechanism/target_capability_audit.md",
            "multi_mechanism/target_capability_contract.md",
        ),
        required=(
            "TARGET_ANCHOR",
            "CANDIDATE",
            "MECHANISM",
            "MECHANISM_RECEIPT",
        ),
    ),
    "FINAL_CHALLENGER": _binding(
        "FINAL_CHALLENGER",
        "final_challenger.md",
        skills=_skills(
            "multi_mechanism/semi_open_audit.md",
            "raw_seed/attack_surface_failure_tests.md",
            "raw_seed/contribution_boundary.md",
            "validation/literature_policy_validation.md",
        ),
        required=("TARGET_ANCHOR", "CANDIDATE"),
    ),
    "FRONTIER_HARDENER": _binding(
        "FRONTIER_HARDENER",
        "frontier_hardener.md",
        skills=_skills(
            "raw_seed/claim_shape_contract.md",
        ),
        required=("TARGET_ANCHOR", "WORK_ITEM", "CANDIDATE"),
    ),
    "BLIND_PROVER_PROBE": _binding(
        "BLIND_PROVER_PROBE",
        "blind_prover_probe.md",
        skills=_skills("raw_seed/attack_surface_failure_tests.md"),
        required=("PROBE_TASK",),
    ),
    "REFUTE_PROVER_PROBE": _binding(
        "REFUTE_PROVER_PROBE",
        "refute_prover_probe.md",
        skills=_skills("critic/raw_seed/attack_counterexamples.md"),
        required=("PROBE_TASK",),
    ),
    "STATEMENT_NORMALIZER": _binding(
        "STATEMENT_NORMALIZER",
        "statement_normalizer.md",
        skills=_skills(
            "raw_seed/claim_shape_contract.md",
            "multi_mechanism/target_capability_contract.md",
        ),
        required=("TARGET_ANCHOR", "CANDIDATE", "INVALID_PROBE_RECEIPT"),
    ),
    "SEMANTIC_DEDUP": _binding(
        "SEMANTIC_DEDUP",
        "semantic_dedup.md",
        skills=_skills(
            "raw_seed/contribution_boundary.md",
            "multi_mechanism/semi_open_audit.md",
        ),
        required=("CANDIDATE_TASK",),
    ),
}


def role_binding(phase: str) -> SmiRoleBinding:
    """Return the exact binding for ``phase`` or fail before model invocation."""

    key = str(phase or "").strip().upper()
    try:
        return ROLE_BINDINGS[key]
    except KeyError as exc:
        raise ValueError(f"unknown SMI phase: {phase}") from exc


__all__ = [
    "SMI_MODE",
    "ROLE_BINDINGS",
    "SmiRoleBinding",
    "SMI_SKILL_CORE_REF",
    "role_binding",
]
