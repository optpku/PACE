# Literal Target Locations

Read the complete user input, including ordered follow-ups. Preserve earlier goals
unless the user changes them, and exclude superseded goals. User messages define
the target; historical agent conclusions are search context.

Return `target_clauses` with only `start_line` and `end_line`, 1-based inclusive
line numbers from source_lines. Select every requested conclusion with its full
formula and qualifiers. Include a whole line when setup and target share it.
The Host copies these original lines into the canonical target contract.
Preserve requested literature reviews, reports and other deliverables as well
as mathematical conclusions. Include explicit limits on scope and any requirement
for, or exclusion of, new proofs, experiments or original research.

If `recommend_mode` in the Host input is true, also return `recommended_mode`
and `mode_reason`. Choose `proof` only when the current request is suitable for
a direct proof attempt under its stated assumptions. Choose `research` for
literature investigations, research reports, open research questions, mechanism
exploration or conjecture refinement. A research request need not seek a new
proof. If uncertain whether direct
proving is appropriate, choose `research`. A precisely stated theorem alone
does not justify `proof`. Do not solve the task, design a proof route, add
assumptions or weaken its conclusion to make it fit direct proving.
`recommended_mode` must be exactly `proof` or `research`; give one short sentence
in `mode_reason`. This recommendation schedules work and grants no proof verdict.
For an explicit requested_mode use that mode. In the same receipt include
initial_context with brief_markdown: one H1 (a specific title in the user's
language, at most 60 characters) and a short paragraph preserving the objects,
assumptions, exact goal, permitted answers, source leads and any independent
proof or reproduction requirement. Point out the first real ambiguity or gap.
In research mode also include initial_context.proof_assessment with difficulty
(routine, standard, research), plan_node_level and dependency_depth_level
(integers 1 compact, 2 substantial, 3 research-scale). In proof mode omit
the assessment. The Host writes the brief. Only the located original lines
establish the target.

## Input
```json
{{host_context}}
```
