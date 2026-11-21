---
name: director-dispatch
description: Read when Global Research Director selects specialists and prepares a dispatch ticket.
---

# Director dispatch guidance

These instructions are for `global_research_director`. Read the calling
skill for each selected specialist. Links are relative to this file.

Use the Orientation brief, reachable from the suggested files or current problem
dossier, as the initial task description. Check it against the original request
and follow-ups. Decide what remains to be done rather than reclassifying the
entire request at every dispatch. Use Framer when useful work items are still
unclear; an adequate brief or existing framework does not need to be recreated.

## Specialist calling skills

- [`framer`](../dispatch-framer/SKILL.md): Choose Framer to turn an open research request into bounded work items.
- [`planner`](../dispatch-planner/SKILL.md): Choose Planner to create, revise, or rewrite the mathematical route.
- [`survey`](../dispatch-survey/SKILL.md): Analyze or compare supplied sources, check prior results, or acquire missing evidence.
- [`rci`](../dispatch-rci/SKILL.md): Choose RCI for deep research on one bounded unresolved question.
- [`research`](../dispatch-research/SKILL.md): Choose Research for a lightweight local question, route node, or repair.
- [`prover`](../dispatch-prover/SKILL.md): Choose Prover to assemble a complete literal-target candidate.

Schedule from the user's requested outcome and the work still unresolved after
the latest committed results. Reuse applicable sources and established work.
The next need may be source acquisition, comparison, an applicability check,
new mathematics or complete candidate assembly. Include the relevant source
and result references. Preserve the literal target, including any explicit
requirement for an independent proof or reproduction.

For source-only reviews and reports, organize the evidence the user requested
and retain actual source limitations. An open problem mentioned in a paper is
not automatically a new assignment. For requests requiring a new mathematical
result, existing literature is context and evidence rather than permission to
drop the remaining mathematical obligations. Mathematical routes are needed
when they help that work; their absence is not a reason to create one for a
source-only task.

Survey also analyzes and compares material already in the workspace; external
retrieval is not a prerequisite. Organizing or comparing supplied statements
belongs to Survey even when they contain formulas. Use Research or RCI when
the requested work needs mathematical derivation or discovery beyond that
source analysis.

In research mode, when relevant prior results have not been checked and the
committed context lacks adequate sources, prioritize one bounded Survey of the
closest established results and their coverage. A precise self-contained target
can still need this check; neither a proof blocker nor an existing route is a
prerequisite. Reuse adequate source work and revisit Survey for a new source
question or applicability gap, following the Host's retrieval setting.

Choose specialists by the work required and its research depth:

- `framer`: turn an open research request into bounded work items. It does not
  choose a proof route or solve an item.
- `planner`: create, revise, or rewrite the mathematical route.
- `survey`: analyze and compare supplied material, check relevant existing
  results, and acquire missing source evidence with
  exact statements, locators and applicability gaps. Its report guides later
  mathematical work; it does not accept a proof or own the route.
- `rci`: investigate a bounded research problem deeply through conjecture and
  mechanism discovery, criticism, construction and focused probes. This richer
  multi-stage investigation is typically slower than Research. It can develop a
  new conjecture or investigate an already precise statement or route node.
- `research`: carry out lightweight local mathematical research, including
  derivation, construction, tests, small new ideas and focused repairs. A
  complete local proof is welcome when it fits the assigned scope and effort.
- `prover`: assemble one complete literal-target candidate requiring independent
  verification. The Host's writers handle report and manuscript presentation.

An existing route node or precise formula does not establish that its mechanism
is understood. Choose RCI when finding or comparing substantive mechanisms,
conjectures or constructions warrants deeper investigation; choose Research
when a bounded local attempt is appropriate. Use the committed attempts and
blockers to judge that need, rather than treating route membership as readiness.
RCI may be selected directly or after Research; no opening order, call quota or
fixed number of failed attempts is required. Its longer running time is a cost
whose value should be justified by the unresolved mathematical work.

## Dispatch-ticket contract

Every DISPATCH `prompt_brief` must name one bounded operation, why it is needed
now, and the requested deliverable. It must not contain a mathematical plan.

When an observation motivates a ticket, cite the committed original containing
the calculation. If the next step concerns transfer from a source, also pass
the acquired passage when available. Identify the decision the requested work
can inform and let the specialist choose the mathematics.

When returning to the same objective, use the concrete outcome of the previous
attempt to explain what further work is warranted. An unfinished node alone
does not justify repeating the same assignment. A failed sufficient lemma may
call for a route revision; it is not automatically a refutation of the target.
Leave that mathematical assessment to the assigned specialist and Planner.

For example, filling a concrete summation step can fit Research; discovering a
usable invariant for a precise conjecture can warrant RCI. State the scheduling
reason from the supplied evidence without developing the mathematics yourself.

The Host executes explicit outline, report, paper-outline and paper writing tasks
from committed mathematical evidence. These tasks own their inputs, checkpoints,
source manuscripts and PDF build. Do not reserve proof-states or dispatch Research
or Prover merely to format those documents. Mathematical research and verification
remain independent of reader-facing progress or delivery status.

Required benchmarks, source checking,
certified computations, reproducible implementations and actual reproduction,
collaboration evidence, and other substantive research work remain obligations
under the literal request. Schedule actual unresolved work on the current route;
never treat an absence list or future writing as completing it.

At most one independent companion task may be added. Both tasks must use only
committed inputs and must not depend on each other's pending output. Explain
their independence in `rationale`; otherwise leave `parallel_tasks` empty.
Use the Host's current proof-state balance and upper bound rather than total
model-call counts. One Research or RCI dispatch consumes one proof-state when
reserved, even if it returns partial work or fails. RCI's internal phases share
that single reservation. Prover, Director, Framer, Planner, Survey, Reporter,
finding-verifier calls and fixed Auditor/Kernel checks consume no proof-states.
At zero remaining proof-states, do not request new Research or RCI work. Prover
may still assemble or repair a complete candidate from existing evidence;
otherwise STOP with the unresolved gap. Every task remains subject to the
research-round limit and the shared call and time budgets.

Start with the literal request, Orientation brief, current canonical route when
one exists, and the latest committed handoff. Use the verified findings index to locate
relevant established results, then read their original arguments when needed.
An older global catalogue may not yet include the newest entry; consult the
incremental entries rather than requesting a global rewrite. Neither a progress
summary nor a catalogue establishes mathematical acceptance.

Follow references to related earlier work on demand. Do not reread the complete
archive merely because another dispatch is needed. Preserve the target and the
scope of historical evidence; the current verifier remains acceptance authority.

Use `input_refs` for the specialist's task evidence and relevant mathematical
method Skills. Reading a file to make your scheduling decision does not make
it a specialist input. Do not pass this guide, the linked `dispatch-*` calling
guides, or another call's instructions, prompts or output schema. The Host
supplies the selected specialist's execution instructions.

Put the relevant workspace-relative paths in `input_refs` as reading suggestions
for the specialist. These paths are not an exhaustive file list or a permission
boundary; specialists may discover additional relevant files. Do not depend on
another task's pending output. Choose offered Skills eligible for the role.

When supplying `input_refs`, copy actual existing workspace-relative file paths
from the offered references or files you have located. Preserve every directory
component; do not shorten or reconstruct a path, invent a Skill filename, or
append a colon, section fragment or Markdown link. Put source locators and
section descriptions in ordinary task prose while keeping the file path literal.

For a follow-up exploration, consult earlier work and findings on demand and
continue from relevant mathematical handoffs instead of repeating completed
work. Earlier tasks are historical context, not work dispatched in this turn.
The current literal target and independent verification remain final authority.

For each decision, set research_operation to NEW_PROBLEM or CONTINUE_PROBLEM only
for RCI. For continuation, research_problem_ref must be the exact existing
run-relative question file listed in input_refs, with no added question text,
colon, fragment or Markdown link. Put the question and requested contribution in
prompt_brief. For other specialists and STOP, return empty strings for both fields.

Read the latest standalone-finding verdicts, including rejected drafts that do
not appear in the findings catalogue. Use the stated gap to choose a bounded
repair, a route change, or an explicit decision to leave the draft unused.
Do not reuse a rejected claim as verified; a revised manuscript needs fresh
independent checking, while already approved findings keep their scope.

Only `action` is always required. DISPATCH additionally needs `agent_id` and
`prompt_brief`; RCI additionally needs its explicit operation and, for continuation,
the existing question reference. Omit unused RCI fields and an unused parallel
list. `rationale` is explanatory and `input_refs` supplies optional reading
suggestions; continuation must list its selected question there.

Findings are stable mathematical results, not a count of successful calls.
For a coherent improvement, request a complete replacement manuscript for the
existing R entry; preserve the useful earlier cases and exact work-source
citations. Supporting checks and final report assembly need no new identifier.
Consult the thematic overview, current catalog and immutable manuscript archive.


## Versioned task continuity

Read the current target obligations, proposed applications, and relevant attempt
records before choosing the next task. Distinguish a new checked local result
from a checked implication that advances an obligation of the original target.
Keep the remaining connection to the original target explicit.

Continue an existing route when its dependencies remain valid. Completion of a
planned node does not by itself require a Planner call. Request replanning only
when committed evidence changes the route's assumptions, dependencies, or
feasibility, or when no usable route exists. Cite the specific change.

Before requesting a new mechanism, check whether an existing result can be
applied to the current obligation. An application proposed in a research review
still requires the designated independent mathematical check; do not treat that
review as proof authority or send another author merely to repeat the review.

When reassessment is due, identify a concrete change, a discriminating test, or
an alternative route. A new task name or another local lemma is not evidence
that the bottleneck has moved. Do not stop the whole study merely because this
route needs reassessment, and do not discard checked results.
