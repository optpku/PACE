---
name: dispatch-rci
description: Choose RCI for deep research on one bounded unresolved question.
---

# Calling rci

Read the [common dispatch guidance](../dispatch.md)
together with these role-specific instructions.

For `rci`, begin with DEEP_RESEARCH and select one bounded unresolved question.
Use NEW_PROBLEM when a question still needs formulation or its statement is
being substantively changed. Use CONTINUE_PROBLEM when investigating an existing
precise question; supply its current research_problem_ref and the relevant
committed attempts. A new method or an unresolved mechanism does not by itself
make a new problem. State why deeper work is useful now and request a concrete
research contribution without prescribing its mathematics.

For continued work, identify the latest unresolved relation and the evidence
available to the specialist. Do not ask for another seed or value assessment
merely because that relation remains open. If scope or route dependencies may
change, ask the specialist to make that change explicit for the Planner.
RCI results and handoff advice guide scheduling; they do not accept a proof.

When consuming an RCI child context, use its prompt-owned
`handoff_disposition` only as scheduling advice:

- `ROUTE_INTEGRATION` suggests Planner when integrating new or changed tasks;
- `LOCAL_EXECUTION` suggests Research when the remaining task fits a lightweight
  local attempt;
- `FULL_CANDIDATE` suggests Prover for complete literal-target assembly.

Read the candidate, mechanism reviews and remaining obligations before choosing
the next specialist. Apply the same depth judgment if the advice is missing or
ambiguous, or if a concrete unresolved mechanism warrants another RCI assignment.
An unchanged node does not automatically require Research, and a missing advice
field does not itself require replanning. Never treat advice as a correctness
verdict.

For each RCI work item, state the fixed question, the single gap to address,
and a concrete return condition in prompt_brief. For repeated exploration,
identify new evidence, a changed parameter/assumption, or a distinct relation
to test. Continue an unchanged question under CONTINUE_PROBLEM; a new method
is not a new question. An obstacle scoped to one mechanism is useful progress.
Request a local repair of named defects before replacing the whole question.
Continuation enters Explorer directly with the existing question and evidence;
it does not recreate a seed or repeat research-value screening. Select the
specific result manuscripts and verification records to reuse within their
scope, and name the one remaining gap and return condition in ordinary prose.
Each new round starts a fresh RCI phase session. Supply the original mechanism,
review and change/gap files needed to continue; session history is not an input.
Keep the accepted question version fixed while stating the new evidence or
different mathematical move for this assignment.
A route-node label alone does not identify an unchanged question. A changed
question uses NEW_PROBLEM with its changed assumptions or conclusion explicit.

An Explorer HANDOFF preserves the question and earlier valid work. Read its
original change.md and named review files, not just NO_ACCEPTED_TASK or an
index. Use the actual first unresolved step and needed source or calculation
to select the next bounded assignment. HANDOFF is not a mathematical rejection;
REJECT concerns the named candidate or mechanism only. Supply the relevant
original work and gap files in input_refs for the next task. Do not dispatch
the identical attempt again without addressing its recorded obstacle.
Check whether completed local arguments should be drafted for independent
finding verification even when the overall exploration is incomplete. A correct
conditional lemma may remain useful while its application conditions are open.


## Versioned task continuity

Preserve the accepted question version when only the method changes. Read the
relevant prior attempt and its exact obstacle before trying the same mechanism.
State what differs in the present attempt: assumptions, parameters, evidence,
method, or the relation being tested. Keep the parent objective and the missing
connection to that objective visible.

A local task is legitimate when its contribution and missing downstream bridge
are explicit. Do not repeatedly enlarge its scope merely because it does not
solve the full target. If the present inputs yield no informative continuation,
save the scoped obstacle and return control rather than repeating the same test.

A failure of this argument is not a refutation of the question. A negative
result applies only to the claim, mechanism, and conditions actually checked.
Keep mathematical changes separate from control-field or formatting repairs.
