# Example: diagnose a missing initial term

A candidate formula for \(E_{k+1}\le qE_k+b\), with
\(0<q<1\), \(b\ge0\), claims \(E_N\le b/(1-q)\) for every
\(N\ge0\), independently of \(E_0\).

At \(N=0\), choose \(E_0>b/(1-q)\); the formula already fails.
The equality sequence
\[
E_N=q^N E_0+b\sum_{j=0}^{N-1}q^j
\]
is admissible and exposes the missing initial term. At \(N=0\) the sum
is empty, and at \(N=1\) the expression is exactly \(qE_0+b\).

The exploration now hands off a precise induction claim with the initial
term retained. Checking a few indices suggests that claim; induction proves
it for all indices. When \(b=0\), the formula reduces to ordinary geometric
decay, another useful boundary check.
