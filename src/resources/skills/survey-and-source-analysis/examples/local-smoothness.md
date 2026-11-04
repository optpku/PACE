# Example: transferring a local estimate

This is an invented source-comparison exercise, not a literature claim.
Suppose an acquired theorem gives
\[
f(y)\le f(x)+\langle\nabla f(x),y-x\rangle+\tfrac L2\|y-x\|^2
\]
when the entire segment \([x,y]\) lies in a specified region \(D\) where
the gradient is \(L\)-Lipschitz. The requested algorithm evaluates a rejected
trial \(y=x-h\nabla f(x)\).

The source supplies the displayed estimate conditionally. Checking \(x\in D\)
alone does not establish \([x,y]\subseteq D\), nor does a theorem about
accepted iterates cover this rejected trial.

A useful Survey handoff identifies the actual source locator, states the
segment hypothesis, and asks for containment of this precise trial segment.
If that bridge is missing, Research can prove it or propose a different safe
step. Survey must not report that the global algorithmic guarantee follows.
