# Complete-candidate verification methods

Read the literal target, current proof and current Auditor report completely.
Use the Plan only as an optional index; include target-critical candidate steps
missing from it and do not invent claim identifiers or require a Plan schema.

Before replay, locate the load-bearing implications, their legal premises and
where the candidate establishes each condition. For one manageable chain,
replay it directly. For several chains, keep bounded local notes within this
same task: record the obligation, proof location, necessary premises, downstream
use and the uncertainty left when a premise is not established. Group routine
steps after checking them and separate difficult joins by mathematical need.

Separate a conditional implication from the proof that its conditions hold.
Keep a coupled bootstrap or continuation argument together when splitting it
would conceal boundary closure. Check representation recovery and joins between
branches whenever they carry the target. Local checked relations retain their
conditions and do not authorize the whole proof.

When recording useful local PASS relations, distinguish a checked reusable
lemma from a step that still depends on this candidate's unverified reasoning.
For an authorized lemma preserve its exact statement, conditions, supporting
dependencies and `allowed_success_contract_components`, and explicitly set
`reuse_authorized=true`. Otherwise set it to false. A conditional implication
can be reused while its application condition remains open; the next candidate
must prove that condition before using its conclusion. An overall MATH_FAIL
does not erase a sound local result, and an old local approval never replaces
the current whole-target and assembly checks.

Checked relations retain their original `target_contract_ref`. A prior turn's
`reuse_authorized` decision belongs to that original scope, even when its problem
identifier matches the current one. Relations listed for reconstruction require
reading their original proof and target, reconstructing the needed argument in
the current candidate, and checking compatibility of assumptions, objects and
application conditions. Memory notes guide this reading; they are not evidence
that these obligations have been discharged.

Every task ends by checking omitted steps, all literal target clauses, condition
compatibility, circular dependencies and final assembly. Do not vote or count
local approvals. Report missing mathematics for repair rather than supplying a
substantive missing proof yourself. These methods are search guidance; the
literal target and checked current candidate determine acceptance.

## Reading index

Read a card when its scope is present or uncertain in the current candidate.
The short-proof card applies to a short chain; arithmetic-and-algebra applies
where a target-critical step uses calculation. Other cards are available within
this same task as needed; their presence is not evidence and unrelated checks
do not require empty certificates.

- `verification_cards/short-proof.md`: replay a compact chain and full coverage.
- `verification_cards/arithmetic-and-algebra.md`: recompute legal transformations.
- `verification_cards/algorithm.md`: oracle, parameter, execution and cost obligations.
- `verification_cards/construction.md`: existence, admissibility and required properties.
- `verification_cards/induction-and-invariant.md`: base, preservation, regional conditions and continuation.
- `verification_cards/rate-and-bound.md`: recurrences, ranges, constants and resource conversions.
- `verification_cards/topology.md`: limits, closedness, qualifications and membership recovery.
- `verification_cards/negative-answer.md`: legal witnesses and the full target negation.
