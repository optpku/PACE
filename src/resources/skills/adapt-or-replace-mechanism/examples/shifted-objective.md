# Example: shift a decomposition without changing the update

This is an illustrative derivation, not a result about the current task.
Let \(F=f+h\), with differentiable \(f\), proper closed convex \(h\),
and \(\eta>0\). For a fixed vector \(c\), set
\(\widetilde f(x)=f(x)-\langle c,x\rangle\) and
\(\widetilde h(x)=h(x)+\langle c,x\rangle\).
The objective is unchanged, but implementability needs a separate calculation.

Completing the square in the proximal minimization gives
\[
\operatorname{prox}_{\eta\widetilde h}(v)
=\operatorname{prox}_{\eta h}(v-\eta c).
\]
Consequently,
\[
\operatorname{prox}_{\eta\widetilde h}
 (x-\eta\nabla\widetilde f(x))
=\operatorname{prox}_{\eta h}(x-\eta\nabla f(x)).
\]
Thus this particular executed update does not require knowing \(c\), even
if \(c\) is chosen using an optimizer in the analysis. An implementation
that separately queries the shifted oracle would still need a legality check.

The adapted proof must also check any smoothness, domain, and constant
conditions it uses. Equality of objectives alone would not prove the update
identity or transfer every property of the decomposition.
