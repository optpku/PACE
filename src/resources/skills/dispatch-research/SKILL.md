---
name: dispatch-research
description: Assign Research a bounded derivation, calculation, construction, applicability check or focused mathematical repair using acquired evidence.
---

# Call Research

Use Research when a focused local attempt can advance the current question:
derive a missing step, construct an object, check applicability, or repair a
concrete gap. A complete local proof is useful within that scope. Compare with
RCI when finding substantive new mechanisms or conjectures warrants deeper work;
a precise formula or route-node label alone does not settle that distinction.

Provide the exact question and assumptions, relevant original evidence, and the
latest attempt or independent review. Ask for actual mathematical work, its
conditions, what it establishes and its first remaining gap. The specialist
chooses its method and uses acquired local sources. A failed sufficient lemma
does not refute the target; use the actual blocker to choose the next attempt.

Set `agent_id` to `research` and begin `prompt_brief` with `EXECUTE_LOCAL_TASK`.
Use the current task manuscript as an input when continuing its bounded question.
Omit `research_operation` and `research_problem_ref`: those fields belong to RCI.
Research reserves one proof-state; complete literal-target assembly belongs to
Prover, while report organization belongs to the Host's writing flow.

Example, when an established route has one explicit algebraic bridge to fill:

```json
{"action":"DISPATCH","agent_id":"research","prompt_brief":"EXECUTE_LOCAL_TASK: Complete the matrix-algebra step in the supplied task. Deliver the explicit parameter conditions, their derivation and any unresolved boundary cases, preserving the stated quantifiers.","input_refs":["work/turn001/round0002/next_task.md","sources/stability_notes.md"],"rationale":"当前缺口是有界推导，已有材料足够支持一次局部研究。"}
```
