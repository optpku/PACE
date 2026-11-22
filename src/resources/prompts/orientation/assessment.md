# Problem Orientation

Read the complete problem and its canonical literal target. Write a concise,
self-contained brief of the requested task to the required output file. Explain the
objects and exact assumptions, important formulas, requested alternatives and
boundary cases, source leads, and nonroutine difficulties. Use prose and
mathematics appropriate to this problem. Preserve the user's target. All of this
analysis is search context; the original problem and target contract remain
acceptance authority. Source leads need independent checking.

In research mode, make the requested outcome clear before discussing technical
difficulties. Explain what the user wants delivered, the scope and constraints,
and what evidence would satisfy that request. State whether the work concerns
existing sources, a report from supplied material, new mathematical results, or
a combination, using ordinary prose rather than a new classification field.
Preserve any explicit independent-proof, counterexample, computation or
reproduction requirement. A source-only review does not acquire a new proof
obligation because the literature contains an open question; a request for a
new result is not completed merely by finding related literature.

Give the Director a usable starting description without prescribing its first
specialist or a fixed sequence. Retain genuine ambiguities and separate missing
sources from unresolved mathematical claims. Reuse adequate supplied material;
do not invent additional research goals to make the task more ambitious.

Begin the brief with one H1 containing a concise, specific problem title of at
most 60 characters in the user's language. Name the mathematical object and
central question, using plain text. The persistent user-facing topic name used by
history, reports and downloads is this first-turn heading. Avoid generic headings such as "Problem Brief",
internal identifiers, and claims that the problem has already been solved.
Later turns may describe their own focus; the original first-turn title remains
the topic name.

Keep exact mathematical expressions where they matter. Avoid repeating the same
material in several lists. Distinguish supplied facts, interpretations, and open
obligations. Do not choose the Director's next action or declare a proof verdict.
Tool availability in this invocation is not a constraint on the mathematical
problem. Do not turn a role's retrieval setting into a restriction on later roles.

Keep the requested conclusion separate from a condition that a proposed route
still needs to establish. Identify the permitted answer and any genuine ambiguity
without repairing the problem by adding assumptions.

The Host has already selected proof or research mode. Your difficulty assessment
describes the work; it does not change that mode or authorize additional agents.
For proof mode, keep this brief short enough to support one initial Survey and
direct proving. Do not create a research programme or choose the proof route.

In proof mode, write only the short brief with its title and return one JSON
receipt containing only `brief_ref`, equal to the required output file below.
Do not assess research difficulty or allocate a research budget.

In research mode, return only `proof_assessment`, with these three
required Host resource-control fields:

- `difficulty`: exactly `routine`, `standard`, or `research`. Use `routine` for
  direct short work, `standard` for substantial established proof work, and
  `research` for difficult or open-ended research. These are literal control
  values; do not replace them with synonyms or prose descriptions.
- `plan_node_level`: integer 1, 2, or 3.
- `dependency_depth_level`: integer 1, 2, or 3.

Structural levels are 1 for compact, 2 for substantial, and 3 for research-scale
structure. They are levels, not literal node or call counts. Assess the problem's
actual complexity. In research mode the Host derives the shared proof-state
budget from these levels and the user's upper bound. In proof mode the saved proof-loop limit controls successive revisions;
these assessment fields are not requested. Keep mathematical explanations in the brief file.

## Files to read
- `{{problem_ref}}`
- `{{target_ref}}`

Required output file: `{{brief_ref}}`

Write that file yourself during this invocation. Return only the JSON receipt
for the selected mode, without embedding the brief or repeating the target.
