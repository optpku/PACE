# Proof Contract Writing Guide

Write one self-contained proof record in prose. Start with the exact literal
target identity, quantifiers, object class, and legal assumptions. Then state
the requested conclusion clause by clause, name the source or verified-relation
reference used for each non-given step, and expose every missing applicability
bridge. A plan, citation, numerical observation, or earlier candidate is a
search input, never a premise.

The record may include a short proof frontier and a failure explanation, but it
must not silently repair hypotheses, broaden scope, rename a target clause, or
claim VERIFIED/PASS/completion. Keep the requested output path in the record
when the role prompt supplies one. The Host stores this prose verbatim; fresh
Auditor and Kernel evidence decides mathematical truth.
