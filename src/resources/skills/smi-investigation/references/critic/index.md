# Raw-Seed Critic Review Guide

Review the exact seed revision adversarially. Mathematical and research reviews
are separate calls and neither substitutes for the other. Judge mathematical
content rather than JSON polish.

## Mathematical review

- Check that the research question is coherent under the copied legal
  assumptions and work-item scope.
- Attempt a concrete first derivation and identify circular, false, undefined,
  or assumption-dependent moves.
- Execute the stated toy case and at least two distinct failure tests.
- Confirm that positive and negative outcomes would be meaningful.

## Research review

- Compare the minimal delta with the closest supplied source boundary by
  assumptions, conclusion, scope, and mathematical move.
- Subtract definitions, notation, routine bookkeeping, and direct applications.
- Confirm that the remaining bottleneck is nontrivial and precise enough for
  Frontier Explorer to investigate without inventing a new question.
- Accept an existing precise question when its mechanism or applicability
  bridge still needs substantive research. A candidate mechanism can be a
  contestable lead; it cannot be an assumed conclusion.
- Carry unresolved source coverage as uncertainty; do not certify novelty from
  absence of a match.

Use `REPAIR` for a bounded defect in the same direction and `REJECT` for a
false, incoherent, routine, out-of-scope or ungrounded route, or when supplied
evidence already closes the actual obligation at the required scope. A known
statement alone does not close a nontrivial missing mechanism or transfer.
Use `ACCEPT` only after the active review has performed concrete work and found
no blocker. Never design the downstream mechanism.
