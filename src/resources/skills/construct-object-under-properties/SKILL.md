---
name: construct-object-under-properties
description: A proof needs an explicit function, sequence, potential, witness, or hard instance.
---

# Construct an object with proved properties

## When to use

A proof needs an explicit function, sequence, potential, witness, or hard instance.

## Inputs

The object type, ambient space/topology, required properties, and the exact downstream use. Use actual supplied files; no special mechanism-pack JSON fields are required.

## Procedure

1. Separate the defining formula from the properties to prove.
2. Establish existence, well-definedness, and membership first.
3. Prove each used property, including any limit or graph-recovery bridge.
4. Insert the object into the consuming relation at the same parameter scope.

## Output and handoff

The construction with property proofs, or a candidate accompanied by its first missing existence, membership, or recovery step. Preserve proved properties without promoting the unfinished object. Write into the artifact requested by the role. Cite the source/proof
references and retain unresolved dependencies. Skill text and worked examples
are methods, not evidence for the current problem. Final acceptance still
requires fresh review of the complete literal-target proof.

## Detailed methods and example

- [Full mathematical procedure](references/workflow.md)

- [Worked example](examples/weighted-potential.md)
