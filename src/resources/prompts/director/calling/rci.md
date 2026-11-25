---
name: dispatch-rci
description: Choose RCI for deep research on one bounded unresolved question.
---

# Calling RCI

Read the [common dispatch guidance](../dispatch.md) with these instructions.

For `rci`, begin with DEEP_RESEARCH. State one bounded question or open
direction, its first concrete gap, and a useful return condition in
`prompt_brief`. Use `NEW_PROBLEM` when the question needs formulation or
the assignment explicitly changes its mathematical statement. Use
`CONTINUE_PROBLEM` with the exact existing `research_problem_ref` in
`input_refs` when the statement stays fixed.

For continued work, send the original mechanism, review and gap files relevant
to the next attempt. Specify new evidence, a different mathematical move, or a
relation to test. For new or revised questions, include any directly relevant
existing candidate statements and acquired source files in `input_refs`.
Choose whole evidence files with their source locators.

RCI starts a new question with Conjecture Explorer and an unchanged question
with Mechanism Explorer. Mechanism reviews can lead to repair, more mechanisms,
a revised conjecture, or a focused independent test. An RCI handoff records the
current question, mathematical originals and a remaining gap. Use those files
to choose further RCI work, local Research, route integration by Planner, or
complete-answer assembly by Prover. A scoped failed approach leaves the original
question open. Independently check any completed local argument, even if its
application to the full target remains unresolved.
