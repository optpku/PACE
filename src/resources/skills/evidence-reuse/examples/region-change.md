# Example: a valid result with an invalid new use

Suppose a recorded proof establishes, for \(x_k\in D\),
\(E_{k+1}\le qE_k\), with \(0<q<1\). A revised method produces
\(z_k\), and its analysis proposes the same rate.

To reuse the result, identify whether the old step equation also holds for
the \(z\)-update, prove \(z_k\in D\) for each needed index, and show that
its energy controls the new target error. With those bridges, induction gives
\(E_N\le q^N E_0\) at the established scope.

If the new update's region is unproved, retain the old contraction theorem.
Mark the application to \(z_k\) conditional and hand off the containment
claim. Declaring the new rate proved because the old finding passed review
would skip exactly this applicability obligation.
