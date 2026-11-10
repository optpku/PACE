# Direct Semi-Open Projection Audit

Audit the prompt-defined child context without trying to solve its open proof.
The projection must come directly from the accepted candidate and frozen anchor.
It contains the statement, formal context, scope, canonical legal assumptions,
positive and refutation targets, selected mechanism refs, and mathematical
validation checks.

## Required checks

- Every legal assumption is copied from the frozen anchor; mechanism outputs,
  target assertions, desired certificates, and refutation witnesses are not
  assumptions.
- Every object, domain, quantifier, and parameter regime used by the statement
  or targets is defined.
- Positive and refutation targets share scope and are genuine duals.
- Selected mechanisms are refs to search context only and do not silently close
  the target.
- Coverage and Challenger artifacts, sources, evidence ledgers, and field-level
  provenance are excluded from the child context.

A structurally complete projection still fails this audit if it smuggles an
assumption, changes the candidate, or turns support into proof.
