---
name: mathematical-claim-preservation
description: Check that PACE's reports and papers retain the exact results, conditions, and evidence scope of the supplied research artifacts.
---

# Checking Research Claims in a Manuscript

Use this Skill when turning a PACE finding into prose or reviewing that prose.
The original question and target contract specify the goal. The committed
findings, proof passages, and applicable verification records specify what
the research established. A previous manuscript, an outline, or editorial
approval cannot replace those inputs.

## Inspect a result before describing it

For each substantive result, open its statement, connected definitions, proof,
and relevant review. Resolve references to the actual files. Where versions
differ, check which statement and proof the committed review addresses;
an older objection does not automatically apply to a repaired argument, and
an approval for one scope does not cover another.

Keep a source location for the checks below while writing or reviewing.
These are working checks, not a new public schema or an extra deliverable.

| Check | What to compare in the source and draft |
| --- | --- |
| Setting | The actual function, sequence, construction, model, or algorithm, including its domain and initialization. An auxiliary object must not take the place of the object being studied. |
| Hypotheses | Admissible parameter ranges, strict or non-strict boundaries, regularity, effective inputs, and any cutoff or region where the claim applies. |
| Quantification | Which choices are fixed and which vary; which parameters a bound, witness, or threshold can depend on. |
| Conclusion | The exact quantity controlled, its norm or mode of convergence, and the strength of the bound or limiting assertion. |
| Argument | The source steps that connect the hypotheses to the conclusion, including essential coefficients, remainder orders, and case coverage. |
| Recorded computation | The executed procedure, tested inputs and range, certificate or enclosure, and recorded validation. A proposed check is not an executed check. |

Read nested quantifiers in their stated order. Check both directions before
describing a one-way implication as an equivalence. An existence argument
does not supply a computable procedure unless the source establishes one;
retain the procedure's actual inputs and stopping conditions. In asymptotic
estimates, compare the leading coefficient and remainder with the source.

A change of notation needs a consistent correspondence to the source objects
throughout statements and proofs. Inspect the saved formulas for altered
operators, signs, subscripts, or escaped commands after any scripted edit.
A source formula that appears mistaken needs an explicit finding; writing
is not the stage for silently repairing its mathematics.

Check shortened arguments using only the steps that remain on the page.
For example, an estimate for a numerator cannot replace the positive margin
needed for a denominator. Reinstate the supplied missing step; if it does not
exist in the source, identify the gap instead of deriving a new result.

## Preserve what the evidence actually answers

Check every shorter description of a result, from the title and abstract to
the conclusion, against the same original statement. Neither stronger nor
weaker wording should obscure the answer relevant to the user's question.
A proved positive lower bound must not disappear into a vague failure claim.
A conditional method that avoids an unavailable input must not acquire that
input in its summary. A construction's size is not an iteration count.

Keep the measured or controlled object explicit. Convergence of function
values, convergence of points, and convergence to a minimizer are different
possible conclusions; use the one supplied by the proof. A bounded sequence
need not converge, and stationarity alone does not establish optimality.
Likewise, a finite
certificate or bounded computation covers its recorded scope, not an
unbounded execution. Preserve any probability qualification attached to an
estimate. Do not describe internal model review as human peer review or
formal verification.

For an external result, open the acquired passage and check that its
hypotheses fit the application being described. Verify authors, titles,
versions, and theorem or page locators. If the necessary passage is missing,
name that specific limitation; do not create a citation or reconstruct a
theorem from memory. Establish a novelty or sharpness claim only from the
actual comparison at the same hypothesis and conclusion scope.

Classify remaining questions by comparing them with both positive results
and counterexamples already in the materials. A disproved universal statement
is settled negatively; a narrower case may still need investigation. A local
theorem may be complete even though the original target remains unanswered.
Keep these distinctions in ordinary mathematical language, without giving
every statement a new internal status label.

## Check coverage across the document

A cumulative Chinese report must account for all still-relevant independent
results in its body or technical appendices. Compare full hypotheses and
conclusions before treating two findings as duplicates. Preserve distinct
rates, parameter dependence, identities, boundary cases, constructions, and
computational guarantees. A title or a link saying that a bound exists does
not state that bound. If one result's alleged coverage of another needs a
new argument, keep their statements separate.

An English paper can select a coherent subset, but it must include the
complete supplied proofs for its selected main results. Locate every required
step in the source and in the paper or its appendices. A research-report
summary or workspace pointer cannot replace a missing paper proof.

Keep necessary evidence references exact. Use source paths, record identifiers,
and recorded values rather than invented validation claims. Do not generate
or repeat checksum claims in manuscript prose. Check already acquired sources and retain exact URLs and locators.
External retrieval is disabled for editorial roles. New experiments,
proofs, and changes to targets or mathematical verdicts belong outside this
editorial task.

## Resolve a discrepancy at its actual scope

When the source contains a step omitted by the draft, restore that step.
When sources leave a material question undecided, describe the affected
conclusion with that limitation. When source records disagree, identify the
statement, version, and review at issue instead of silently choosing a
convenient summary. A specific uncertainty does not invalidate every other
result in the run.

Read revision feedback in full and check its diagnosis against the original
evidence. Correct the affected argument and its summaries together, removing
superseded sentences. Record any justified alternative in the assigned
revision response, with manuscript and source locations. Recheck the whole
revised document for lost results as well as the previously reported issues.
The writer supplies the requested revision; the independent editor judges
that document, and the Host controls the next workflow step.

## Supporting material

- [Worked procedure and checks](examples/claim-drift.md)
