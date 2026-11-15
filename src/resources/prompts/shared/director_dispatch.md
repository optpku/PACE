# Director dispatch protocol

Return one JSON object matching the supplied schema. DISPATCH selects `agent_id`,
states a bounded task and deliverable in `prompt_brief`, and supplies its directly
needed `input_refs`. Write `rationale` as a concise Chinese scheduling reason.
STOP omits dispatch fields. Omit fields unused by the chosen role; only RCI uses
`research_operation` and `research_problem_ref`. Calling skills show role-specific
briefs and examples; substitute actual current paths for their illustrative ones.

Use exact run-root-relative paths in reference fields: no Markdown links, prose,
page numbers or `#section` suffixes. Put locators and their meaning in the brief
or `route_reason`. `archive_refs` may name existing background files or directories;
`route_evidence_refs` must name existing files. An RCI continuation question must
be an existing file also selected in `input_refs`.

Select current manuscripts, definitions and independent reviews as direct inputs.
Put optional history and full old routes in `archive_refs`; the Host keeps full
route manuscripts on demand for non-Planner tasks. Use `route_relations.json`,
`memory/research_index.json` and Survey's `source_index.json` to locate original
evidence. Navigation and DELIVERED assignment tickets do not establish closure.
Use the latest independently reviewed statement after a repair. Preserve blind
review isolation.

When the Host requires route assessment, return `route_assessment` KEEP or REFRESH,
`route_reason`, and `route_evidence_refs` in the same decision. Compare the route's
live obligations with all subsequent findings, following the original evidence
and reviews. REFRESH when a load-bearing branch is closed or invalidated, or a
verified reduction changes the remaining problem, assumptions or dependency chain;
dispatch Planner alone before downstream work. KEEP when the map still represents
the remaining work, explaining the precise bridge. Merely retaining the same
problem name or adding a lemma does not settle that judgment. For STOP with no
remaining route work use KEEP and explain the stopping decision.

At most one independent companion may appear in `parallel_tasks`. Both tasks use
committed evidence, never each other's pending output; explain their independence
in `rationale`. Every task consumes one research round. Research and RCI each
reserve one proof-state, even on partial or failed attempts; RCI's phases share
that reservation. At zero proof-states, choose other useful permitted work or STOP.
The Host enforces the round limit and Survey's four-call acquisition limit.
With one Survey call remaining, Survey can only be primary. Reuse covered material.

If the Host requests receipt correction, read the previous response and correct
all reported control/reference defects in one JSON response. Preserve the valid
routing action, selected specialists, bounded task and mathematical content.
Use the selected calling skill for missing role fields. This is one local
format correction, not a new research decision or a proof verdict.
