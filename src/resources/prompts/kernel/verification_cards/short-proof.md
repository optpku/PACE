# short-proof

The candidate appears to be one short chain or a single target-critical
derivation. Short length does not lower the verification burden.

Write the literal target clauses and quantifiers first. List each nontrivial
relation in dependency order, replay its actual premises, and mark the exact
line or definition that supports it. Check the first and last index, empty or
zero cases, denominators and sign changes, then substitute the checked
relations back into every target clause. A compact proof may group routine
algebra only after it has been checked; “obvious”, “similarly”, or a repeated
conclusion cannot replace a missing bridge.

Stop at the first unsupported implication or uncovered target clause. Return
the relation, its missing premise, the affected downstream clauses and the
smallest repair obligation.
