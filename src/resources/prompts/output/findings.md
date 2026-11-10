# Standalone research findings

When your assigned work actually establishes a useful mathematical result,
write a standalone Chinese Markdown manuscript in the Host-specified finding
draft directory, using a descriptive filename ending in `.md`. Draft only
results supported by mathematics already completed in this task. No draft is
required when you only have a plan, a conjecture, an incomplete proof or a
literature-search gap. Do not manufacture a result to fill the directory.

Before drafting, read `results/findings/README.md` when available, then the
relevant original manuscripts. The overview is navigation, not mathematical
authority. State the actual increment over existing material. Mere restatement,
a change of notation, reformatting, transcription, routine formula rechecking,
or reusing an unchanged result in a later turn does not establish a new finding:
cite the existing material instead. Keep useful research experience in memory.
An explicit request to reproduce a benchmark or provide an independent
certificate can justify a manuscript; explain that supporting purpose.

Keep successive intermediate steps of one coherent result in its task manuscript
by default. Write a separate finding for an independently useful lemma, a
substantive strengthening, a genuinely different condition or rate, or a new
proved counterexample. Reference the related earlier result and explain the
mathematical difference. A fresh numbering or a standalone transcription alone
is not an increment. Do not overwrite or withdraw earlier findings yourself.

Each manuscript should contain its title, exact statement, definitions and
assumptions, a complete proof or counterexample, its relation to the original
problem, dependencies and exact sources. You may precisely cite an existing
independently verified dependency without copying its proof: identify its exact
statement, conditions and manuscript location, and prove that its application
conditions hold here. Fully prove the new increment, including any gluing,
realization or consistency argument; several dependency manuscripts do not
automatically establish a new assembled conclusion. A pointer to a larger
unfinished argument is insufficient. Preserve explicit conditions and
distinguish literature results from this task's own work.

Positive lemmas, bounds, constructions and conditional theorems are useful, as
are proved counterexamples, impossibility results and precise route limitations.
Explain exactly which statement a negative result refutes. Failure to prove a
claim is not a refutation.

Use ordinary Markdown links relative to the draft file for local supporting
files, and use only external URLs present in your inputs. Your manuscript will
be independently checked before publication. Do not write verification verdicts
or edit `results/`, shared state, another task's drafts or previously published
findings. A later strengthening is a new manuscript, with an explicit reference
to the result it strengthens.

For executions using the Host finding-inventory contract, write the requested
`finding_manifest.json`. Identify the current manuscript of each result and any
explicit old-to-new replacement, with a short reason. Keep superseded drafts as
history. Do not present them as current deliverables or overwrite them. An
inventory entry only selects a version for independent checking; it grants no
mathematical approval and cannot withdraw a previously published result.
Submit the inventory in the same delivery as the manuscripts. If the Host asks
for an inventory correction, change only its file references and explicit
version relationships; keep the mathematical files unchanged. Report ambiguity
instead of inventing a replacement or selecting a version by mathematical merit.
