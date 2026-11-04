# Example: a gap before a valid recurrence solution

A candidate has established \(E_{k+1}\le E_k-a r_k\), where
\(a>0\), but then writes \(E_{k+1}\le(1-a\mu)E_k\) without
proving \(r_k\ge\mu E_k\).

The recurrence solution after that point may be correct conditionally. The
first blocker is the error comparison, not the telescoping algebra. Return
the exact comparison, the region where it is needed, and the assumptions that
might imply it. Preserve the descent estimate at its established scope.

If an admissible family has \(r_k/E_k\to0\) while the permitted parameters
remain fixed, it may refute a uniform positive \(\mu\). That refutes this
linear-rate bridge. It does not alone refute every convergence rate or the
original algorithmic target. The next task must address that distinction.
