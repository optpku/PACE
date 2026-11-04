# Example: solve the recurrence and retain the residual floor

Suppose \(E_k\ge0\), \(0<a<1\), \(b\ge0\), and
\(E_{k+1}\le(1-a)E_k+b\) for every integer \(k\ge0\).
Iterating, or inducting from \(N=0\), gives
\[
E_N\le(1-a)^NE_0+b\sum_{j=0}^{N-1}(1-a)^j
=(1-a)^NE_0+\frac ba\bigl(1-(1-a)^N\bigr).
\]
The induction multiplies the previous bound by the nonnegative \(1-a\)
and adds \(b\). The base is equality and the empty sum is zero.

For \(b>0\), this estimate has a residual floor \(b/a\); it does
not guarantee convergence to zero. If \(E_0>\varepsilon>b/a\),
it suffices to take
\[
N\ge\left\lceil
\frac{\log((E_0-b/a)/(\varepsilon-b/a))}{-\log(1-a)}
\right\rceil.
\]
If \(E_0\le\varepsilon\), the initial point already qualifies. At or
below the floor, this upper bound generally yields no finite guarantee.
Constants known only in the proof do not automatically make this stopping
horizon executable. Iteration-to-oracle conversion is a separate accounting
step. If the recurrence was proved only in a region, prove containment before
applying it for all \(N\).
