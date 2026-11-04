---
name: construct-counterexamples
description: An exact target clause or a proposed intermediate lemma appears false.
---

# Construct counterexamples

## When to use

An exact target clause or a proposed intermediate lemma appears false.

## Inputs

The challenged statement with all quantifiers, the literal target separately, and every witness admissibility condition. Use actual supplied files; no special mechanism-pack JSON fields are required.

## Procedure

1. Write the logical negation, retaining the residual quantifiers.
2. Choose a small structured family and verify all hypotheses symbolically.
3. Compute the challenged quantity and demonstrate a strict violation.
4. State whether the witness refutes the local route, a restricted claim, or the full target.

## Output and handoff

A complete admissible witness or no-witness proof at the required scope. Otherwise return the unmet premise or missing target-level reduction; failed search is inconclusive. Write into the artifact requested by the role. Cite the source/proof
references and retain unresolved dependencies. Skill text and worked examples
are methods, not evidence for the current problem. Final acceptance still
requires fresh review of the complete literal-target proof.

## Detailed methods and example

- [Full mathematical procedure](references/workflow.md)
- [Obstruction method](references/obstruction-method.md)
- [Worked example](examples/route-versus-target.md)
