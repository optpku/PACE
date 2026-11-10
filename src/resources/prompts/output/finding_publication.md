# Decide how this checked manuscript belongs in the findings

Mathematical verification and publication organization are separate decisions.
A finding identifier names one mathematical result that can be improved over
time. Its current file may change; its checked originals and receipts remain
at their exact work paths. Mathematical PASS does not require a new identifier.
Neither a publication decision nor an editorial overview grants proof reuse or
final acceptance of the original target.

After the mathematical check, use `results/findings/catalog.md` and the relevant
part of `results/findings/README.md` when present. Follow related original
manuscripts; `results/findings/archive.md` is an on-demand version lookup, not a
required full-history read. Receipts identify scope and provenance but cannot
replace your independent mathematical check. Explain the actual
mathematical increment and its relation to the existing result, using judgment
rather than counts, scores or a requirement of literature novelty.

Return a `publication` object with one of these actions and a concise reason:

- `CREATE`: a substantively different, independently useful mathematical result.
  A new theorem, construction or important limitation may qualify even when it
  is partial or conditional. New realization, gluing and consistency proofs can
  establish a new result even when all their ingredients were already checked.
- `UPDATE`: improve an existing result under its stable identifier. Include
  `finding_ref`, the exact current path such as `results/findings/R0001.md`.
  The current manuscript must be the complete replacement text: keep the useful
  earlier statements and proofs, or precisely cite their immutable checked
  originals and establish applicability. Check the complete assembled replacement
  as well as the new argument. Proof repairs, better constants, weaker hypotheses
  and coherent extensions normally belong here. Preserve distinct assumption
  regimes explicitly; do not call incomparable claims a stronger theorem.
- `NONE`: retain this manuscript and its check without changing current findings.
  Use this for supporting calculations, routine rechecks, benchmark reproductions,
  literature matching, intermediate proof steps without an independent result,
  and reports assembling existing results. Also use it when a mathematical
  increment has not been established or an UPDATE draft omits necessary old
  content. Do not label sound mathematics MATH_FAIL for an editorial omission.

For example:
`{"action":"UPDATE","finding_ref":"results/findings/R0001.md","reason":"The complete checked replacement weakens the curvature hypothesis and preserves the prior case."}`
or `{"action":"NONE","reason":"This is an integrated report of existing results, without a new mathematical increment."}`.

An unresolved comparison is not evidence of a new result. Preserve the manuscript
without adding an identifier. There is no finding quota. A genuinely new complete
answer can be CREATE; final report assembly alone must be NONE. The final
Kernel's `publication_eligible` still concerns acceptance of the complete answer
and is independent of this findings action. For a finding-verifier PASS without
standalone reuse authorization, use NONE.

You do not edit published files or original inputs. The Host executes this explicit
decision, persists the exact checked text and provenance, and updates the current
entry only after verification. Cite immutable source paths when binding a proof
to a particular version; the stable R path is the current reading entry.
