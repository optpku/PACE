---
name: refine-bounds-rates-and-constants
description: A valid progress relation needs to become an error bound, rate, constant, or resource estimate.
---

# Refine bounds, rates, and constants

## When to use

A valid progress relation needs to become an error bound, rate, constant, or resource estimate.

## Inputs

The one-step relation, its valid region and parameter range, the target error, and the actual cost model. Use actual supplied files; no special mechanism-pack JSON fields are required.

## Procedure

1. Prove the comparison and region in which the recurrence is legal.
2. Solve the actual recurrence with its base, coefficient directions, and endpoint.
3. Convert the result to the requested error and accuracy range.
4. Count all charged operations; compare sharpness claims only at matching class and oracle scope.

## Output and handoff

The derived finite-horizon or asymptotic statement with constant dependencies, or the exact missing comparison, continuation, or charging lemma. Retain residual floors and conditional warm-start assumptions. Write into the artifact requested by the role. Cite the source/proof
references and retain unresolved dependencies. Skill text and worked examples
are methods, not evidence for the current problem. Final acceptance still
requires fresh review of the complete literal-target proof.

## Detailed methods and example

- [Full mathematical procedure](references/workflow.md)
- [Recurrence method](references/recurrence-method.md)
- [Worked example](examples/contraction-with-error.md)
- [Oracle cost accounting](references/oracle-cost-accounting.md)
- [Failure example: coefficient direction](examples/invalid-variable-coefficient-bound.md)
- [Local validity and continuation](../direct-proving-and-gap-closure/references/continuation-method.md)
