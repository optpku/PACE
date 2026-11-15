---
name: dispatch-planner
description: Assign Planner to create or refresh a mathematical route when the route is missing or evidence changes its live obligations and dependencies.
---

# Call Planner

Use Planner when mathematical work needs a route that is absent, or committed
evidence changes its obligations, assumptions or dependency chain. Compare the
current map with cumulative findings and their reviews. A closed branch or a
verified reduction may change the remaining problem even if its broad name stays
the same. A new lemma, elapsed round or unfinished node alone does not require
replanning; useful bounded work may continue on an unchanged map.

Provide the literal target, current route and dependency map, and the specific
evidence motivating change. Put full proofs and obsolete route detail in the
archive. Expect a compact current route, `route_relations.json` and one concrete
`next_task.md`, preserving every live obligation and linking its scoped evidence.
Planner owns the mathematical strategy; let it choose the actual dependencies.

Set `agent_id` to `planner`. Begin the brief with `CREATE_ROUTE` when no usable
route exists, `REVISE_ROUTE` for a bounded change, or `REWRITE_ROUTE` when the main
approach is invalidated and the impact cannot be kept local. A substantive route
milestone uses REFRESH and Planner alone before downstream exploration.

Example, after independently reviewed evidence invalidates a route branch:

```json
{"action":"DISPATCH","agent_id":"planner","prompt_brief":"REVISE_ROUTE: Incorporate the reviewed counterexample, retain the user's unresolved classification goal, and deliver the revised dependency map and next task.","input_refs":["work/turn001/input/problem.md","work/turn001/round0002/route.md","results/findings/R1.md","results/findings/R1_review.md"],"route_assessment":"REFRESH","route_reason":"The reviewed counterexample invalidates a load-bearing universal branch; the classification remains open.","route_evidence_refs":["results/findings/R1.md","results/findings/R1_review.md"],"rationale":"已有核验成果改变了剩余依赖，需要先更新路线。"}
```
