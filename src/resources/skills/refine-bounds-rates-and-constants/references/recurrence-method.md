# Recurrence method

**Do not claim:** a rate from an asymptotic pattern or from substituting a constant coefficient without proving the one-step relation and invariant region. State the base, legal index range, coefficient direction, endpoint, and terminal conversion explicitly.

**Handoff:** return the first missing comparison or recurrence relation with its exact scope.

Start from the target quantity and available one-step equations. Build a
candidate potential by combining terms whose cross terms can cancel or whose
errors can be absorbed. Derive the coefficient equations and allowable signs;
state which parameters are computable and which are analysis-only. A potential
name or desired contraction formula does not supply its one-step proof.

Prove nonnegativity, the initial value and the relation between the potential
and target error. If the comparison is local, derive it conditionally and then
close its region by induction, bootstrap or continuation before applying it
throughout the recurrence. Keep variable coefficients in the recurrence until
a justified comparison replaces them; preserve the direction of that comparison.

Solve the actual recurrence over its valid index and parameter range. Separate
warm-start and asymptotic phases when their estimates differ, and carry every
remaining parameter into the endpoint. Convert iterations to charged operations
using the actual updates. If a coefficient or cancellation cannot be established,
return that concrete failed equation rather than assuming the requested rate.
