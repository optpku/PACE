"""Execute a bounded conjecture and mechanism investigation."""

from pathlib import Path

from pace.artifacts.workspace import _write_json_atomic, is_rci_critic_material
from pace.runtime.prompts import render_prompt_template

from .phase_runner import BudgetedPhaseRunner, RciBudgetExhausted, RciPhaseError
from .roles import AUTHORS, PHASE_ACTIONS


class RciPipeline:
    def __init__(self, *, phase_runner):
        self.phase_runner = phase_runner

    def run(
        self,
        *,
        workdir,
        round_root,
        round_index,
        target_contract_id,
        target_anchor_ref,
        work_item_ref,
        research_operation,
        research_problem_ref,
        source_refs,
        independent_source_refs,
        related_candidate_refs,
        mechanisms,
        task_brief,
    ):
        root = Path(workdir)
        attempt = Path(round_root)
        runner = BudgetedPhaseRunner(self.phase_runner, root=root, directory=attempt)
        ordinal = 0
        published = []
        evidence = []
        manuscripts = {}
        critic_derivations = {}
        alternatives = list(related_candidate_refs)
        mechanisms = dict(mechanisms)
        candidate = research_problem_ref
        draft = ""
        base = ""
        next_phase = (
            "MECHANISM_EXPLORER"
            if research_operation == "CONTINUE_PROBLEM"
            else "CONJECTURE_EXPLORER"
        )

        def call(phase, inputs, *, actions=(), routing=None):
            nonlocal ordinal
            ordinal += 1
            phase_name = phase.lower()
            directory = attempt / f"{ordinal:03d}_{phase_name}"
            task = render_prompt_template(f"rci/{phase_name}/task.md", workdir=root)
            if phase in AUTHORS:
                task += f"\n\nAssigned research objective: {task_brief}"
            result = runner.run(
                phase=phase,
                workdir=root,
                call_dir=directory,
                target_contract_id=target_contract_id,
                input_refs=inputs,
                prompt_seed=task,
                round_index=round_index,
                allowed_actions=actions,
                routing_context=routing,
            )
            published.extend(result.artifact_refs)
            payload = result.payload
            originals = payload.get("manuscript_refs", ())
            for ref in originals:
                manuscripts[ref] = payload.get("dependency_refs", [])
            if phase in AUTHORS:
                reviews = list(
                    dict.fromkeys(
                        ref
                        for ref in (*source_refs, *evidence)
                        if is_rci_critic_material(ref)
                    )
                )
                if reviews:
                    mechanism = (
                        payload.get("mechanism_ref")
                        if phase == "MECHANISM_EXPLORER"
                        else None
                    )
                    for ref in (payload["change_ref"], mechanism, *originals):
                        if ref:
                            critic_derivations[ref] = reviews
            return payload

        anchor = {"TARGET_ANCHOR": target_anchor_ref, "WORK_ITEM": work_item_ref}

        def research_inputs():
            return {
                **anchor,
                "CANDIDATE": candidate,
                "SOURCE": source_refs,
                "EVIDENCE": evidence,
            }

        def review_mechanism(ref, *, previous=""):
            review = call(
                "MECHANISM_CRITIC",
                {
                    **research_inputs(),
                    "MECHANISM": ref,
                    "PREVIOUS_MECHANISM": previous,
                    "RELATED_MECHANISM": [item for item in mechanisms if item != ref],
                },
                routing={"candidate_ref": candidate, "mechanism_ref": ref},
            )
            evidence.append(review["review_ref"])

        status = "HANDOFF"
        reason = "research_handoff"
        failure = {}
        try:
            while True:
                if next_phase == "MECHANISM_EXPLORER":
                    proposal = call(
                        next_phase,
                        {
                            **research_inputs(),
                            "MECHANISM": tuple(mechanisms),
                            "BASE_MECHANISM": base,
                        },
                        routing={
                            "candidate_ref": candidate,
                            "base_mechanism_ref": base,
                            "mechanism_ids": mechanisms,
                        },
                    )
                    evidence.append(proposal["change_ref"])
                    if proposal["action"] in {"HANDOFF", "CLAIM_RESOLVED"}:
                        if proposal["action"] == "CLAIM_RESOLVED":
                            reason = "candidate_resolution"
                        break
                    if proposal["action"] == "PROPOSE_MECHANISM":
                        ref = proposal["mechanism_ref"]
                        identity = proposal["mechanism_id"]
                        mechanisms = {
                            item: value
                            for item, value in mechanisms.items()
                            if value != identity
                        }
                        mechanisms[ref] = identity
                        review_mechanism(
                            ref, previous=proposal.get("base_mechanism_ref", "")
                        )
                    next_phase = "CONJECTURE_EXPLORER"
                    base = ""
                    continue

                actions = PHASE_ACTIONS[next_phase]
                if not candidate:
                    actions = ("PROPOSE_CONJECTURE", "HANDOFF")
                elif not mechanisms:
                    actions = tuple(
                        action for action in actions if action != "REVIEW_MECHANISM"
                    )
                decision = call(
                    next_phase,
                    {
                        **research_inputs(),
                        "MECHANISM": tuple(mechanisms),
                        "RELATED_CANDIDATE": alternatives,
                        "DRAFT_CANDIDATE": draft,
                    },
                    actions=actions,
                )
                evidence.append(decision["change_ref"])
                action = decision["action"]
                if action == "HANDOFF":
                    break
                if action == "PROPOSE_CONJECTURE":
                    draft = decision["candidate_ref"]
                    related = tuple(
                        dict.fromkeys(ref for ref in (*alternatives, candidate) if ref)
                    )
                    review = call(
                        "CANDIDATE_CRITIC",
                        {
                            **anchor,
                            "CANDIDATE": draft,
                            "RELATED_CANDIDATE": related,
                            "SOURCE": independent_source_refs,
                        },
                        routing={"candidate_ref": draft},
                    )
                    evidence.append(review["review_ref"])
                    if review["action"] == "ACCEPT":
                        critic_derivations[draft] = [review["review_ref"]]
                    if review["action"] in {"ACCEPT", "REUSE"}:
                        selected = (
                            review["existing_candidate_ref"]
                            if review["action"] == "REUSE"
                            else draft
                        )
                        if candidate and candidate != selected:
                            alternatives.append(candidate)
                        candidate = selected
                        draft = ""
                        next_phase = "MECHANISM_EXPLORER"
                elif action == "EXPLORE_MECHANISM":
                    base = decision.get("mechanism_ref", "")
                    next_phase = "MECHANISM_EXPLORER"
                elif action == "REVIEW_MECHANISM":
                    review_mechanism(decision["mechanism_ref"])
                else:
                    phase = (
                        "BLIND_PROVER_PROBE"
                        if action == "PROBE_PROOF"
                        else "REFUTE_PROVER_PROBE"
                    )
                    probe = call(phase, {"PROBE_TASK": decision["probe_task_ref"]})
                    evidence.append(probe["review_ref"])
        except RciBudgetExhausted as exc:
            reason = str(exc)
        except RciPhaseError as exc:
            reason = exc.reason_code or "rci_interface_failed"
            if reason not in {"required_input_missing", "required_input_file_missing"}:
                status = "INTERFACE_FAILED"
            failure = {
                "phase": exc.phase,
                "message": str(exc),
                "diagnostics": exc.diagnostics,
            }

        outcome = attempt / "outcome.json"
        _write_json_atomic(
            outcome,
            {
                "outcome": status,
                "reason": reason,
                "candidate_ref": candidate,
                "evidence_refs": evidence,
                "published_refs": list(dict.fromkeys(published)),
                "manuscript_refs": list(manuscripts),
                "manuscript_dependencies": manuscripts,
                "critic_derivations": critic_derivations,
                "failure": failure,
            },
        )
