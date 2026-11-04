# Explaining How an Estimate Answers the Question

Use a proof's mathematical dependency to organize its presentation. The
example below is a writing exercise, not evidence about the current run.

## Example: an energy estimate and a residual bound

Suppose the source gives nonnegative sequences \(V_k\) and \(r_k\), a
constant \(a>0\), and the inequality
\[
V_{k+1}+a r_k\leq V_k \qquad (k\geq 0).
\]
For every integer \(N\geq1\), the source establishes
\[
\min_{0\leq j<N}r_j\leq \frac{V_0}{aN}.
\]

Explain the mechanism: the decrease of \(V_k\) pays for the accumulated
residuals, and nonnegativity limits the total available decrease. Present the
calculation rather than merely saying that a telescoping argument applies:
\[
a\sum_{k=0}^{N-1}r_k
 \leq \sum_{k=0}^{N-1}(V_k-V_{k+1})
 = V_0-V_N
 \leq V_0.
\]
The smallest of these \(N\) residuals is at most their average, so
\[
\min_{0\leq j<N}r_j
 \leq \frac{1}{N}\sum_{k=0}^{N-1}r_k
 \leq \frac{V_0}{aN}.
\]
The stated bound concerns the best residual in that prefix. It does not
give the same estimate for the last residual, and it does not identify a
limit of an underlying iterate sequence.

## Apply the presentation method to supplied results

In the actual manuscript, obtain every inequality and conclusion from its
assigned proof source. Introduce the controlled quantity and hypotheses
before explaining how the argument uses them. State which intermediate
estimate pays for which part of the conclusion. Keep constant dependencies,
indices, and the range of a statement visible when summarizing it.

A Chinese report may summarize a long derivation and link to the complete
checked proof, but it must still state the resulting guarantee. An English
paper selecting the result must include its full supplied argument. An
appendix is appropriate for lengthy calculations when the body explains
their role and points to their exact location.

If the proof requires a lemma introduced elsewhere, place its statement or
an exact reference before its use. After moving material, check definitions
and references in reading order. If only a sketch exists, identify the
missing step at that result; the example above does not authorize completing
an unrelated proof during manuscript writing.
