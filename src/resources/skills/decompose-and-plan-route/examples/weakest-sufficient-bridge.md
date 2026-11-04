# Example: plan for a best-iterate residual

Given \(V_k,r_k\ge0\), \(a>0\), and
\(V_{k+1}+ar_k\le V_k\), the target is
\(\min_{k<N}r_k\le V_0/(aN)\), for \(N\ge1\).

A sufficient route has three nodes:

1. Sum the given one-step relation: \(a\sum_{k<N}r_k\le V_0-V_N\).
2. Use \(V_N\ge0\) to bound the sum by \(V_0/a\).
3. Compare the smallest of \(N\) terms with their average.

Each node consumes only the stated premises or an earlier node. There is no
need to prove monotonicity of \(r_k\) or a last-iterate estimate. For example,
\(r_0=0,r_1=1\), \(V_0=V_1=a,V_2=0\) satisfies the two steps while
the residual increases. Such a case falsifies an unnecessary monotonicity
bridge but leaves the required best-iterate conclusion intact.

If the actual task instead asks for a last-iterate bound, this route is
incomplete; the plan must expose the additional comparison as an obligation.
