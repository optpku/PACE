---
name: dispatch-framer
description: Assign Framer to turn an open mathematical research request into bounded work items before a mathematical route can be chosen.
---

# Call Framer

Use Framer when an open mathematical request lacks usable, bounded objectives.
Provide the literal request, fixed assumptions, desired outputs and relevant
context. Expect work items with explicit scopes, dependencies and completion
targets. Framer clarifies what needs answering; Planner decides how to approach it.

Set `agent_id` to `framer` and begin `prompt_brief` with `FRAME_WORK_ITEMS`.
Ask for a bounded work-item framework without prescribing its mathematical route.
A clear literature question can go directly to Survey; organizing sufficient
existing material belongs to the Host's writing flow.

Example, for an open mathematical request whose objectives need delimiting:

```json
{"action":"DISPATCH","agent_id":"framer","prompt_brief":"FRAME_WORK_ITEMS: Delimit the user's open stability question into explicit mathematical objectives, dependencies and completion targets while preserving the original scope.","input_refs":["work/turn001/input/problem.md"],"rationale":"研究问题尚未形成可执行的有界目标。"}
```
