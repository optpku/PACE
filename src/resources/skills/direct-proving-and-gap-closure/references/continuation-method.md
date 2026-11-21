# Close coupled dependencies

Use this method when an estimate requires membership in a region and the
estimate is also meant to establish that membership. Distinguish deriving the
conditional lemma from applying it at points proved admissible.

Define the safe region from available premises or an explicitly conditional
assumption, independently of the assertion that all future iterates stay there.
Specify only the points, segments and bounds the estimate actually uses. State
the local lemma as an implication with those conditions, and derive it inside
that scope without asserting future containment.

Establish a safe base. Choose a closure mechanism that fits the objects:
induction for discrete updates, a virtual-step family for varying step size, or
a first-exit argument for a continuous process. Define the family and prove the
needed continuity, existence and boundary attainment; continuity of a point does
not automatically give continuity of a nonsmooth objective evaluated there.

For a first-boundary proof, derive the local conditions on the safe interval
and at its boundary. Apply the conditional lemma there and show that its
conclusion contradicts the boundary value, typically through a strict margin.
State why a boundary exists if exit occurs and handle a base already on the
boundary. For discrete induction, prove that admissible state k licenses the
estimate which gives admissible state k+1. Do not assume the latter membership
in order to prove it; use an auxiliary safe family when needed.

Only after closure apply the estimate at the full actual step and derive the
invariant, terminal implication and subsequent dependencies. Auxiliary points
need only the properties consumed by the proof; a comparison variable never
queried by the oracle need not inherit unrelated query restrictions.

If closure fails, identify its exact residue: absent safe base, unavailable
endpoint bound, unproved continuity, lack of a strict boundary contradiction or
an estimate still requiring the future invariant. The conditional lemma may
remain useful, but it does not establish the original unconditional claim.

**Handoff:** provide the conditional estimate, base, closure argument and final
invariant with conditions at each application, or the precise unclosed link
and its downstream effect. These remain local derivations for reconstruction
and independent final verification.
If closing that link needs an unknown mechanism rather than a local derivation,
include the concrete attempts and explain to Director why deeper RCI research
would help; the conditional lemma alone does not establish the missing closure.
