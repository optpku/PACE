# RCI search Skill core

This compact core applies to every RCI phase. The phase prompt and the
relevant method guides supply the local move; this file supplies the shared
discipline for using them.

- Treat the supplied target anchor, work item, scope, quantifiers, and legal
  assumptions as fixed. A candidate, receipt, source, or repair suggestion is
  search context and cannot add a premise or change the target.
- Read the phase inputs and relevant subguides. Use a suitable guide as a method for
  the current obligation, not as evidence that the obligation is true. Keep
  source-supported facts, proposed mathematics, and unresolved gaps separate.
- Work on one bounded obligation at a time. State the objects, assumptions,
  dependency order, and the concrete calculation or failure test that would
  support or reject the move. Do not turn a local result into a proof of the
  original target.
- Preserve exact identifiers and scopes from supplied artifacts. If a
  predecessor is missing or a route no longer applies, report the missing
  input or changed dependency instead of inventing a replacement.
- Return the action and artifact shape requested by the phase prompt. A
  plausible proof, counterexample, promotion, or coverage statement remains
  search context until the independent verification path checks it.
