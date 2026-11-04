# Example: bound the element actually used by the proof

Let \(h(x)=M|x|\), \(f(x)=Mx\), and \(M>0\), on the real line.
Then \(F=f+h\) is minimized at \(x_*=0\) (and at every \(x\le0\)).
At zero, \(\partial h(0)=[-M,M]\) contains the bounded witness \(0\).
But the Fermat element is
\[
-\nabla f(0)=-M\in\partial h(0),
\qquad |\nabla f(0)|=M.
\]
Existence of a zero-norm subgradient does not bound the selected element
\(-\nabla f(0)\). Any claimed bound independent of \(M\) based only
on that existential witness has a gap.

A repair must prove a bound on the selected element, establish a universal
bound on the whole subdifferential, or avoid using that bound. This example
does not itself establish an algorithmic lower bound: such a conclusion needs
its own oracle and target-scope argument.
