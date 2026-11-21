---
name: failure-diagnosis
description: Convert a failed derivation, source transfer, experiment, or review into a scoped mathematical repair task.
---

# Diagnose the first failed implication

## When to use and inputs

Use after a mathematical blocker or an inconclusive attempt. Read the current
candidate, the actual review or failed calculation, and the literal target.
An API error or missing transport response belongs to runtime recovery; it
supplies no mathematical evidence about the conjecture.

## Procedure

1. Locate the first unsupported implication, naming its premises and conclusion.
2. Distinguish an absent derivation, a false intermediate claim, a source-scope
   mismatch, an inadmissible example, and a genuine target-level counterexample.
3. Preserve independently supported upstream results. Identify downstream uses
   invalidated by this blocker, including changed conditions and constants.
4. Choose a repair that changes the failed ingredient: derive a bridge, weaken
   an unnecessary intermediate claim, replace a mechanism, or develop a witness.
5. Give the next role an exact obligation, needed files, and a falsification
   check. Explain when deeper RCI work is warranted.

## Output and handoff

Return the failed relation, evidence, scope reached, affected consumers, and
the smallest useful next task. Recommendations go to Director; Planner owns
route updates, and Prover writes a complete current candidate. A failure report
or patched paragraph does not replace fresh whole-proof verification.

- [Diagnosis and repair guide](references/repair.md)
- [Example: an unproved comparison](examples/unsupported-comparison.md)
