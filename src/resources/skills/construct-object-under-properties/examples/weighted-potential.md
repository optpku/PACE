# Example: construct a potential by cancelling an error term

Suppose nonnegative sequences satisfy
\[
u_{k+1}\le q u_k+c v_k,\qquad v_{k+1}\le r v_k,
\quad c\ge0,\quad 0\le r<q<1.
\]
Seek \(V_k=u_k+\alpha v_k\). To obtain \(V_{k+1}\le qV_k\),
it suffices that \(c+\alpha r\le q\alpha\). The explicit choice
\(\alpha=c/(q-r)\) is nonnegative and satisfies equality. Therefore
\[
V_{k+1}\le q u_k+(c+\alpha r)v_k=qV_k,
\qquad u_N\le V_N\le q^N V_0.
\]

The construction includes the definition, sign, one-step relation, and
connection to the target quantity. If \(q=r\) and \(c>0\), this
choice is undefined and no finite \(\alpha\) satisfies the same inequality.
That defeats this constant-weight ansatz, not every possible bound or potential.
