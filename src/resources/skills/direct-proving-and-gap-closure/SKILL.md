---
name: direct-proving-and-gap-closure
description: A well-scoped claim has enough structure for a direct derivation or a focused proof repair.
---

# Prove a statement or close a gap

## When to use

A well-scoped claim has enough structure for a direct derivation or a focused proof repair.

## Inputs

The exact target and hypotheses, relevant proof/source files, and the latest blocker when repairing. Use actual supplied files; no special mechanism-pack JSON fields are required.

## Procedure

1. Identify the nearest missing implication between premises and conclusion.
2. Expand the load-bearing derivation with constants, quantifiers, and conditions at use.
3. Check selected witnesses, local validity, boundary cases, and the final implication.
4. For a whole proof, rewrite one complete current candidate and map each target clause to its argument.

## Output and handoff

An insertable local derivation or complete candidate as assigned. If the bridge fails, give the exact equation or unmet hypothesis and recommend the next mathematical action; do not rename the gap as a lemma. Write into the artifact requested by the role. Cite the source/proof
references and retain unresolved dependencies. Skill text and worked examples
are methods, not evidence for the current problem. Final acceptance still
requires fresh review of the complete literal-target proof.

## Detailed methods and example

- [Full mathematical procedure](references/workflow.md)
- [Continuation method](references/continuation-method.md)
- [Proof handoff](references/proof-handoff.md)
- [Worked example](examples/selected-subgradient.md)
