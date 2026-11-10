---
name: rci-investigation
description: Develop a candidate statement and its supporting mechanism through seed review, support review, coverage checks, and independent probes.
---

# RCI investigation

## When to use and inputs

Recommend RCI when a bounded research question needs conjecture formation,
comparison of mechanisms, or a nontrivial new construction. Start from the
target anchor, assigned work item, acquired sources, and the inputs supplied
to the current phase. The phase prompt owns the requested files and receipt.

## Procedure and method map

1. **Form a precise candidate.** State objects, assumptions, quantifiers,
   conclusion, and refutation target. Read [claim shape](references/raw_seed/claim_shape_contract.md)
   and [source gaps](references/raw_seed/source_gap_seed_discovery.md).
2. **Review the seed.** Separate mathematical plausibility from research
   value. Read [critic guidance](references/critic/index.md) and
   [contribution boundaries](references/raw_seed/contribution_boundary.md).
3. **Develop a mechanism.** Expose intermediate claims and their independent
   support. Read [mechanism construction](references/mechanism/proof_mechanism.md)
   and [interfaces](references/multi_mechanism/capability_interface_contract.md).
4. **Check support and coverage separately.** A justified lemma may still be
   insufficient for the candidate. Read [mechanism review](references/multi_mechanism/mechanism_audit.md)
   and [coverage](references/multi_mechanism/bundle_closure_audit.md).
5. **Challenge and revise.** Use independent statement-only probes to expose
   easy proofs, counterexamples, or ambiguous formulations. Revise the mechanism,
   candidate, or decomposition in response to the actual failed relation.
   Read [challenge review](references/multi_mechanism/semi_open_audit.md).

RCI uses acquired sources; only Survey can retrieve external sources.
Blind phases use their supplied probe statement for independent searches;
their local workspace inputs remain limited to the probe task and static
methods. Never consult hidden candidate mechanisms, source leads, reviews,
sibling work, or history through this library.

## Output and handoff

Write the phase's requested original mathematical artifacts and return their
references using its existing receipt format. Keep statement manuscripts
separate from mechanism and review files. Conditional candidates retain their
added assumptions; they do not replace the original target. Support/coverage
receipts remain research guidance, with final acceptance reserved for the
complete proof's fresh Auditor/Kernel review.

- [Shared phase discipline](references/rci_search_core.md)
- [Example: repair a mechanism or revise a claim](examples/support-versus-coverage.md)
