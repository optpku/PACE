---
name: construct-algorithm-under-contract
description: The task asks for a method with specified legality, convergence, or cost guarantees.
---

# Construct an algorithm under the target

## When to use

The task asks for a method with specified legality, convergence, or cost guarantees.

## Inputs

The instance class, legal inputs and oracles, requested output/error, and available mathematical relations. Use actual supplied files; no special mechanism-pack JSON fields are required.

## Procedure

1. Separate computable inputs and observations from proof-only quantities.
2. Specify initialization, every update, branch, restart, and stopping rule in the proof.
3. Prove legality and invariant before using the rate; count rejected as well as accepted trials.
4. Assemble every requested guarantee, including endpoints and the actual charged operations.

## Output and handoff

A complete algorithm and proof in the assigned artifact, or an explicit missing legality, invariant, termination, or accounting lemma. Follow the role output contract; do not invent a second control schema. Write into the artifact requested by the role. Cite the source/proof
references and retain unresolved dependencies. Skill text and worked examples
are methods, not evidence for the current problem. Final acceptance still
requires fresh review of the complete literal-target proof.

## Detailed methods and example

- [Full mathematical procedure](references/workflow.md)

- [Worked example](examples/bisection.md)
