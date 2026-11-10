# Example: support is different from coverage

Consider a candidate asserting that every continuous coercive
\(f:\mathbb R^d\to\mathbb R\) has a unique minimizer. A proposed
mechanism proves compactness of a suitable sublevel set and applies the
extreme value theorem.

The mechanism can support existence: choose the sublevel at \(f(0)\),
which is nonempty and compact, attain its minimum, and note that points
outside it have larger values. This argument does not support uniqueness.

An independent statement-only challenge can use
\(f(x)=(x^2-1)^2\) on \(\mathbb R\). It is continuous and coercive,
and has two distinct minimizers. The original uniqueness candidate is false.

One revised candidate adds strict convexity. That condition gives uniqueness:
if distinct minimizers \(x,y\) existed, strict convexity at their midpoint
would give a value below the minimum. Existence still uses the earlier
coercivity argument. The revised statement is conditional on an additional
assumption and cannot be presented as proving the original claim.

This example illustrates the method only. During an actual blind probe, do
not reveal the proposed existence mechanism or previous review to the probe.
