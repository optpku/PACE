---
name: director-dispatch
description: Read when Global Research Director selects specialists and prepares a dispatch ticket.
---

# Director dispatch guidance

These instructions are for `global_research_director`. Read the calling
skill for each selected specialist. Links are relative to this file.

## Specialist calling skills

- [`framer`](../dispatch-framer/SKILL.md): Choose Framer to turn an open research request into bounded work items.
- [`planner`](../dispatch-planner/SKILL.md): Choose Planner to create, revise, or rewrite the mathematical route.
- [`survey`](../dispatch-survey/SKILL.md): Choose Survey for a bounded prior-result check or source need.
- [`smi`](../dispatch-smi/SKILL.md): Choose SMI for deep research on one bounded unresolved question.
- [`research`](../dispatch-research/SKILL.md): Choose Research for a lightweight local question, route node, or repair.
- [`prover`](../dispatch-prover/SKILL.md): Choose Prover to assemble a complete literal-target candidate.

Schedule from the work still unresolved after the latest Survey and committed
results. Reuse applicable established results and dispatch the remaining
derivation, applicability check or bridge. Include the supporting source and
result references with the next ticket. Preserve the literal target, including
any explicit requirement for an independent proof or reproduction.

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
- `survey`: check relevant existing results and acquire source evidence with
  exact statements, locators and applicability gaps. Its report guides later
  mathematical work; it does not accept a proof or own the route.
- `smi`: investigate a bounded research problem deeply through conjecture and
  mechanism discovery, criticism, construction and focused probes. This richer
  multi-stage investigation is typically slower than Research. It can develop a
  new conjecture or investigate an already precise statement or route node.
- `research`: carry out lightweight local mathematical research, including
  derivation, construction, tests, small new ideas and focused repairs. A
  complete local proof is welcome when it fits the assigned scope and effort.
- `prover`: assemble one complete candidate proof or report requested by the
  current route.

An existing route node or precise formula does not establish that its mechanism
is understood. Choose SMI when finding or comparing substantive mechanisms,
conjectures or constructions warrants deeper investigation; choose Research
when a bounded local attempt is appropriate. Use the committed attempts and
blockers to judge that need, rather than treating route membership as readiness.
SMI may be selected directly or after Research; no opening order, call quota or
fixed number of failed attempts is required. Its longer running time is a cost
whose value should be justified by the unresolved mathematical work.

## Dispatch-ticket contract

Every DISPATCH `prompt_brief` must name one bounded operation, why it is needed
now, and the requested deliverable. It must not contain a mathematical plan.

When returning to the same objective, use the concrete outcome of the previous
attempt to explain what further work is warranted. An unfinished node alone
does not justify repeating the same assignment. A failed sufficient lemma may
call for a route revision; it is not automatically a refutation of the target.
Leave that mathematical assessment to the assigned specialist and Planner.

For example, filling a concrete summation step can fit Research; discovering a
usable invariant for a precise conjecture can warrant SMI. State the scheduling
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
model-call counts. One Research or SMI dispatch consumes one proof-state when
reserved, even if it returns partial work or fails. SMI's internal phases share
that single reservation. Prover, Director, Framer, Planner, Survey, Reporter,
finding-verifier calls and fixed Auditor/Kernel checks consume no proof-states.
At zero remaining proof-states, do not request new Research or SMI work. Prover
may still assemble or repair a complete candidate from existing evidence;
otherwise STOP with the unresolved gap. Every task remains subject to the
research-round limit and the shared call and time budgets.

Start with the literal request, the current canonical route, and the latest
committed mathematical handoff. Use the verified findings index to locate
relevant established results, then read their original arguments when needed.
An older global catalogue may not yet include the newest entry; consult the
incremental entries rather than requesting a global rewrite. Neither a progress
summary nor a catalogue establishes mathematical acceptance.

Follow references to related earlier work on demand. Do not reread the complete
archive merely because another dispatch is needed. Preserve the target and the
scope of historical evidence; the current verifier remains acceptance authority.

Put relevant workspace-relative paths in `input_refs` as reading suggestions
for the specialist. These paths are not an exhaustive file list or a permission
boundary; specialists may discover additional relevant files. Do not depend on
another task's pending output. Choose offered Skills eligible for the role.

For a follow-up exploration, consult earlier work and findings on demand and
continue from relevant mathematical handoffs instead of repeating completed
work. Earlier tasks are historical context, not work dispatched in this turn.
The current literal target and independent verification remain final authority.

For each decision, set research_operation to NEW_PROBLEM or CONTINUE_PROBLEM only
for SMI. For continuation, research_problem_ref must be the exact existing
run-relative question file listed in input_refs, with no added question text,
colon, fragment or Markdown link. Put the question and requested contribution in
prompt_brief. For other specialists and STOP, return empty strings for both fields.

Read the latest standalone-finding verdicts, including rejected drafts that do
not appear in the findings catalogue. Use the stated gap to choose a bounded
repair, a route change, or an explicit decision to leave the draft unused.
Do not reuse a rejected claim as verified; a revised manuscript needs fresh
independent checking, while already approved findings keep their scope.

Only `action` is always required. DISPATCH additionally needs `agent_id` and
`prompt_brief`; SMI additionally needs its explicit operation and, for continuation,
the existing question reference. Omit unused SMI fields and an unused parallel
list. `rationale` is explanatory and `input_refs` supplies optional reading
suggestions; continuation must list its selected question there.

Findings are stable mathematical results, not a count of successful calls.
For a coherent improvement, request a complete replacement manuscript for the
existing R entry; preserve the useful earlier cases and exact work-source
citations. Supporting checks and final report assembly need no new identifier.
Consult the thematic overview, current catalog and immutable manuscript archive.
