---
name: mathematical-exposition
description: Present PACE's research answers and proof mechanisms in a readable Chinese report or English paper, with the appropriate technical detail for each deliverable.
---

# Explaining the Research Outcome

Use the present task and its requested document as the writing brief.
Research logs and historical drafts supply context; they do not prescribe
the new document's organization. Explain the outcome to a mathematician who
has the relevant background but has not followed the PACE run.

## Make the answer visible

Start with the original question, the answer obtained, the conditions that
matter, and the principal case still undecided. Where the materials contain
a counterexample, explain exactly which assertion it defeats. Describe the
relationship to known work using the acquired sources, without treating a
newly generated proof as automatically novel.

Organize the body by mathematical questions and results. For each section,
make clear which part of the research question it addresses and how its
conclusion will be used. Several findings may belong in one argument; one
finding may contain results serving different parts of the document. Rebuild
that organization when necessary, rather than keeping the run chronology and
adding a summary in front of it.

## Choose detail for the assigned output

| Document | Body | Technical material |
| --- | --- | --- |
| Chinese research report | Cover the independent results of the run, their actual guarantees, assumptions, implications, and key reasoning. | Retain useful identities, bounds, constructions, and computational conclusions in organized appendices. Long proofs may be summarized with exact links to their complete checked sources. |
| English mathematical paper | Explain the selected contribution and the argument connecting its main results. | Include the full available proofs for selected main results, in the body or appendices. Internal file references alone cannot supply those proofs. |

Do not replace a useful result by a bare statement that some estimate or
algorithm exists. State the estimate or guarantee and the hypotheses that
give it meaning, even if its long derivation belongs in an appendix. Preserve
distinct results when their assumptions or conclusions differ. Source logs
and abandoned versions need not be reproduced.

## Explain the mechanism using the supplied argument

Introduce the relevant objects and assumptions, state the result, and then
show how the source argument reaches it. Identify what an auxiliary estimate
controls and why that control is sufficient at the next step. A list of
actions such as "construct, estimate, verify" does not explain that connection.

Give an auxiliary lemma its own statement when later reasoning needs that
result or it has independent mathematical value. Keep a calculation inside
the proof when naming it separately adds no useful structure. Make the
dependencies visible: the reader should know which earlier hypothesis,
estimate, or cited theorem justifies a transition. Recover any necessary
calculation from the source rather than hiding it behind an assertion of
obviousness. If the source has a gap, report it through claim-preservation
review; do not complete new mathematics during exposition work.

Check definitions in the document's final reading order. A symbol moved from
an appendix into the body needs its meaning there. A reused letter must not
silently combine different source objects. Explain notation changes and keep
temporary variables local to the argument using them. State the dependencies
of a technical constant near its definition; its expanded calculation can go
in an appendix if the result remains understandable without that expansion.

Use numbered references for results and equations used later. Explain what a
long display establishes and which subsequent conclusion needs it. When
sections move, update their references and transitions together.

## Keep presentation and provenance readable

Use mathematical subjects in the narrative: the sequence, bound, parameter
range, theorem, or unresolved implication. For a Chinese report, for example,
"尚未证明该估计对所有初值成立" identifies the missing assertion more clearly
than an unexplained workflow status. The example illustrates wording only;
it supplies no fact about the present research.

State a necessary limitation with the result it restricts. Consolidate
repeated qualifications, and move execution history and detailed source
identifiers to a concise evidence appendix when needed. Keep the reader's
attention on what was established and why.

Render report formulas with supported Markdown math delimiters, such as
\(...\) and \[...\]; reserve code spans for literal commands and paths.
Report evidence links must resolve from the assigned output file. In papers,
local evidence identifiers are monospaced text, not relative PDF hyperlinks.
Follow the task's source-file and citation requirements.

## Inspect the document in reading order

The writer rereads the opening and body before checking the appendices and
sources. The independent editor does the same without first borrowing the
answer from research records or old reviews. The body should reveal the
answer, its boundaries, and its mechanism on that first reading. Then check
the full document for missing results, unsupported transitions, ambiguous
notation, and inaccurate references.

A revision must repair the affected passage and its dependencies, rather
than append a correction while leaving the misleading text in place. Check
that restructuring has retained the independent content of the sources.
Assess the supplied document itself; page count, file existence, and earlier
approval do not determine its quality. The editor writes concrete findings
to the assigned review and the routing decision to the assigned decision
file. Only the Host advances or publishes the deliverable.

## Supporting material

- [Worked procedure and checks](examples/proof-presentation.md)
