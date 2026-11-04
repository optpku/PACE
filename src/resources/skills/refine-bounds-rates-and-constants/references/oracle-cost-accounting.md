# Count the operations the algorithm actually performs

Define the unit first: gradient call, function query, proximal solve, or an
explicit combination. For each accepted update count its initial, accepted,
rejected, and cleanup calls. Explain whether an observed value is reused or
recomputed. An iteration bound alone is not an oracle bound.

For a charging argument, name the events and a nonnegative quantity charged by
each. Prove a positive charge per counted event, disjointness or controlled
reuse, and a finite bound on total charge. For example, if each of \(J\)
events increases a monotone level by at least \(\delta>0\), and its total
increase is at most \(B\), then \(J\delta\le B\). If a restart resets
that level, count the new initial budget for each restart; do not reuse the
same \(B\) across all phases without proof.

Rejected local-model trials must be legal before their certificates are used.
Establish region and segment containment independently of the estimate that
the rejected trial was supposed to validate. State rollback behavior and the
cost of obtaining any certificate used to accept or reject it.

Add the initial and terminal operations and invert the rate only in its valid
accuracy range. A bound depending on an unknown optimizer, initial gradient
bound, or regional radius may be a useful conditional theorem while failing
to provide an implementable stopping rule. Label that distinction explicitly.
