# Example: an executable guarantee for bisection

Let \(f\) be continuous on \([a_0,b_0]\), with
\(f(a_0)\le0\le f(b_0)\). Assume an exact sign oracle and requested
bracket width \(\varepsilon>0\). These are the legal inputs.

If an endpoint is a root, return it. Otherwise query the midpoint; return it
when its value is zero, or retain the half with opposite endpoint signs.
Continuity preserves a root in each retained interval. After \(k\) nonterminal
updates its width is \((b_0-a_0)/2^k\).

If \(b_0=a_0\), the endpoint assumptions give a root immediately.
Otherwise, at most
\[
K=\max\{0,\lceil\log_2((b_0-a_0)/\varepsilon)\rceil\}
\]
midpoint queries produce a bracket of the requested width. Count endpoint
queries separately if their values were not supplied. Its midpoint has distance
at most \(\varepsilon/2\) from a root in that bracket.

This guarantee is a distance-to-some-root guarantee, not a bound on
\(|f(x)|\) without additional regularity. With a noisy sign oracle, the
branch invariant needs a different proof. The example shows why the oracle,
output error, edge case, invariant, and operation count must agree.
