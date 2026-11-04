---
name: explore-toy-and-boundary-cases
description: A sign, index, scale, constant, or structural claim is uncertain.
---

# Explore toy and boundary cases

## When to use

A sign, index, scale, constant, or structural claim is uncertain.

## Inputs

The candidate relation, admissible class, parameter range, and the specific uncertainty being tested. Use actual supplied files; no special mechanism-pack JSON fields are required.

## Procedure

1. Choose the smallest admissible family and include zero, first, and endpoint cases.
2. Compute exact expressions where possible; retain numerical precision and tested ranges otherwise.
3. Extract a symbolic obstruction or a precise candidate general relation.
4. Hand the general relation to proving, or an admissible violation to counterexample construction.

## Output and handoff

The family, exact calculation or bounded experiment, and what it suggests next. Successful finite tests do not prove a universal claim; an inadmissible violation is diagnostic only. Write into the artifact requested by the role. Cite the source/proof
references and retain unresolved dependencies. Skill text and worked examples
are methods, not evidence for the current problem. Final acceptance still
requires fresh review of the complete literal-target proof.

## Detailed methods and example

- [Full mathematical procedure](references/workflow.md)

- [Worked example](examples/endpoint-and-scale.md)
