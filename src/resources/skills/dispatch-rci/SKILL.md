---
name: dispatch-rci
description: Assign deep RCI exploration of conjectures, substantive mechanisms or constructions, and specify whether to formulate a new question or continue an existing one.
---

# Call RCI

Use RCI when a bounded mathematical uncertainty warrants deeper conjecture
formation, mechanism discovery or comparison, or a nontrivial construction.
Its multi-stage investigation is slower than Research; choose it for the actual
uncertainty, without requiring a quota of failed preliminary attempts. A precise
statement can still have no usable mechanism. RCI uses acquired local sources.

Provide the fixed question, assumptions, relevant source evidence, earlier
attempts, independent reviews and the concrete remaining gap. Expect candidate
statements, supported mechanisms, independent criticism, local results and a
precise handoff. These guide research; they do not accept the literal target.

Set `agent_id` to `rci`, begin `prompt_brief` with `DEEP_RESEARCH`, and choose:

- `NEW_PROBLEM` when the question needs formulation or its statement changes
  substantively. Preserve the user's target and supply the evidence defining the
  new scope; leave `research_problem_ref` unused.
- `CONTINUE_PROBLEM` for the same existing question. Set `research_problem_ref`
  to its current reviewed `next_task.md` or equivalent exact question file, also
  in `input_refs`. Use the latest reviewed version after a repair. A new method
  does not create a new question; a whole route or change log is not that question.

Example of formulation:

```json
{"action":"DISPATCH","agent_id":"rci","research_operation":"NEW_PROBLEM","prompt_brief":"DEEP_RESEARCH: Formulate and investigate a bounded mechanism question for the unresolved switching regime, preserving the original stability target and using the supplied source evidence.","input_refs":["work/turn001/input/problem.md","sources/coverage.md"],"rationale":"剩余困难需要形成并比较实质性机制。"}
```

Example of continuing a fixed question:

```json
{"action":"DISPATCH","agent_id":"rci","research_operation":"CONTINUE_PROBLEM","research_problem_ref":"work/turn001/round0002/next_task.md","prompt_brief":"DEEP_RESEARCH: Continue the supplied fixed question from its latest reviewed attempt. Investigate the unresolved mechanism and deliver supported progress plus the exact remaining gap.","input_refs":["work/turn001/round0002/next_task.md","work/turn001/round0002/review.md"],"rationale":"问题范围未变，但现有机制仍不能支持所需量词。"}
```

One RCI dispatch reserves one proof-state; its internal phases share it. At zero
remaining credits, choose another useful permitted task or STOP.
