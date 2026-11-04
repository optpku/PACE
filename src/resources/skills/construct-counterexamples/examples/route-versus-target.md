# Example: refuting a bridge without refuting its target

The target asks whether every nonnegative summable sequence tends to zero.
A proposed route claims that every such sequence is monotone.

Define \(a_{2j}=0\), \(a_{2j+1}=2^{-j}\) for integers \(j\ge0\).
The sequence is nonnegative and its sum is \(2\). It is not monotone:
\(a_0=0<a_1=1\), whereas \(a_1>a_2=0\). This admissible witness
refutes the route's monotonicity claim.

It does not refute the target. Indeed \(a_k\to0\). For the general
target, if some \(\varepsilon>0\) were exceeded infinitely often,
nonnegativity would make partial sums arbitrarily large. Summability excludes
that possibility, so only finitely many terms exceed each \(\varepsilon\).

The correct handoff replaces the monotonicity bridge with this tail argument.
Writing down a nonmonotone sequence and declaring the target false would
confuse the challenged intermediate statement with the requested conclusion.
