# Refine Bounds, Rates, and Constants

# Input Contract

- Read the authorized baseline descent, progress, recurrence, or comparison theorem.
- Record every baseline condition, parameter range, and region of validity.
- Read the exact target gap, residual, progress quantity, rate, and resource measure.
- Read any structural inequality supplied by the problem or verified context.
- Read verified dependency relations at their exact condition and conclusion scope.
- Read the supplied source and mechanism files: the notation correspondence, parameter choices, intermediate derivations, hypotheses at each use, and origins of constants.
- Treat candidate constants and numerical patterns as screening evidence until derived.
- Do not broaden a local inequality into a global statement without an invariant-region proof.

# Procedure

1. Define the target error, residual, gap, or progress quantity exactly.
2. Restate the baseline one-step relation with all constants and conditions.
3. Apply the supplied symbol and parameter substitutions and preserve the stated origin of every constant.
4. Derive the selected ordered intermediate relations under their exact conditions at use.
5. State any supplied construction before relying on its recurrence, potential, or comparison property.
6. Identify the missing comparison that converts baseline progress to target decrease.
7. Derive the applicable structural inequality from authorized assumptions.
8. Examples include problem-specific growth, error-bound, sharpness, or invariant relations.
9. State the exact local or global region in which the comparison is valid.
10. Prove the continuation or invariant that keeps the iterates or objects in that region and yields the terminal implication.
11. Determine the legal step-size, parameter, horizon, and confidence range.
12. Substitute the comparison into the baseline relation without changing inequality direction.
13. Define any Lyapunov quantity, prove its one-step comparison, and connect it to the target error before telescoping.
14. Solve the actual variable-curvature recurrence with its base value and endpoint conventions.
15. Do not replace a variable coefficient by a bound whose inequality direction weakens rate growth.
16. In particular, M_k >= M0 does not imply the A_N lower bound obtained by setting M_k=M0.
17. For changed-center rejected and accepted trials, derive a perturbation or displacement comparison.
18. Derive high-curvature accepted-trial and rejected-trial counts from proved progress quantities.
   The count must be a checkable lemma: define the charged events, prove each event consumes a
   nonnegative increment of a named potential or monotone level, prove the total potential
   variation, and account for the initial and terminal levels. Do not count a rejected probe using
   a certificate evaluated at a point whose region membership has not yet been established.
   When a local certificate depends on `||x-y||` or an observed gradient, prove the comparison
   uniformly along the whole segment before substituting it into the recurrence.
19. Telescope or iterate while preserving constants, exponents, and logarithmic factors.
20. Translate the recurrence into the requested rate quantity.
21. Check the zero-horizon and first-update cases before applying an asymptotic recurrence or dividing by the horizon.
22. Invert the rate to an iteration bound only over the stated accuracy range, with integer rounding and endpoint conventions explicit.
23. Convert iterations to oracle calls or other resources using the actual call accounting.
24. State the additive overhead and prove its comparison in the intended small-epsilon regime.
   Separate an honest conditional warm-start bound from an unconditional complexity theorem. If
   the initial gradient, level-set radius, or regional containment is theorem data rather than a
   legal input/derived quantity, keep it in the proof-only condition and do not call the resulting
   horizon computable.
25. Check candidate constants on boundary and structured near-worst cases.
26. Separate a valid improved upper bound from an independent lower-bound sharpness claim.
27. Compare upper and lower results only after matching instance class, oracle, resource measure, method scope, randomness, dimension or horizon, and error quantity.
28. Do not call a conditional upper bound matching merely because a broader lower bound has the same asymptotic expression.
29. State whether the result is local, global, conditional, asymptotic, or finite horizon.
30. Connect the refined conclusion to the exact target or algorithm contract clause.

# Handoff and Failure Discipline

- Hand off a doubtful constant, endpoint, or regime to toy and boundary exploration.
- Hand off a missing comparison inequality to direct gap closure.
- Hand off a source-based comparison with mismatched scope to mechanism adaptation.
- Hand off a required extremal object or lower-bound family to object construction.
- If the baseline conditions fail, do not repair the rate by silently adding them.
- If only a local region is proved, report and use the local result at that scope.
- Keep lower-bound matching separate from the upper-bound recurrence that defines the method.
- Reuse a verified lower bound as a scoped dependency; do not reconstruct it inside the upper bound.
- Finite tests cannot prove tightness, global optimality, or the best possible constant.
- A stronger rate requires a complete derivation, not a renamed asymptotic expression.
- Final rate and complexity authority remains with the verified complete proof.
