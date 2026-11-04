# Literal Target Locations

Read the complete user input, including ordered follow-ups. Preserve earlier goals
unless the user changes them, and exclude superseded goals. User messages define
the target; historical agent conclusions are search context.

Return `target_clauses` with only `start_line` and `end_line`, 1-based inclusive
line numbers from source_lines. Select every requested conclusion with its full
formula and qualifiers. Include a whole line when setup and target share it.
The Host copies these original lines into the canonical target contract.

If `recommend_mode` in the Host input is true, also return `recommended_mode`
and `mode_reason`. Choose `proof` only when the current request is suitable for
a direct proof attempt under its stated assumptions. Choose `research` when
mechanism exploration, conjecture refinement or other substantial exploratory
work is needed before a proof can be assembled. If uncertain whether direct
proving is appropriate, choose `research`. A precisely stated theorem alone
does not justify `proof`. Do not solve the task, design a proof route, add
assumptions or weaken its conclusion to make it fit direct proving.
`recommended_mode` must be exactly `proof` or `research`; give one short sentence
in `mode_reason`. This recommendation schedules work and grants no proof verdict.
If `recommend_mode` is false or absent, return only `target_clauses`; the Host
has already selected the mode. Do not change that selection.

Mathematical exposition belongs in the
Orientation brief produced by the following role.

## Input
```json
{{host_context}}
```
