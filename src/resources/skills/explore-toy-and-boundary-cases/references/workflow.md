# Explore Toy and Boundary Cases

# Input Contract

- Read the active relation, target assumptions, parameter ranges, and current blocker.
- Identify which uncertainty concerns structure, indexing, constants, signs, or scope.
- Use only examples that respect the active domain and explicitly record any relaxed condition.
- Treat finite calculations, symbolic examples, and numerical witnesses as search evidence.
- Use every such calculation as screening evidence, never as a premise by itself.
- Do not replace a universal proof with a collection of tested instances.
- For Research, keep exploration tied to the one canonical blocker.
- When a finite experiment would clarify that blocker, formulate one bounded exploratory
  calculation in ordinary prose with its admissible family, parameter range, quantities,
  and falsification condition; do not create a separate request object or status record.

# Procedure

1. Write the exact candidate relation or construction property being screened.
2. List the assumptions that every admissible test must satisfy.
3. Choose the smallest meaningful dimension, horizon, or canonical special form.
4. Include degenerate and boundary values that exercise quantifiers and conventions.
5. Test index starts, terminal indices, empty sums, zero parameters, and limiting regimes.
6. Check sign, homogeneity, scaling, units, and monotonicity where they constrain the formula.
7. Evaluate candidate constants on structured extremal or near-extremal families.
8. Separate a violation of the active relation from failure of the full target conclusion.
9. If a numerical witness is verifier backed, reproduce its exact values independently.
10. Extract the symbolic identity, missing hypothesis, or corrected parameter range it suggests.
11. Formulate the resulting bridge or obstruction as an exact mathematical statement.
12. Return to theoretical derivation and prove the general relation from authorized premises.
13. Check that the proposed correction does not reintroduce a removed condition implicitly.
14. Retest the corrected relation on the cases that exposed the original weakness.
15. Record unexplored regions instead of claiming global sharpness from a finite screen.
16. Compare alternative conventions when the target has fixed one only implicitly.
17. Preserve exact arithmetic or symbolic forms when floating-point behavior could mislead.

# Handoff and Failure Discipline

- Hand off a stable symbolic pattern to direct proving and gap closure.
- Hand off a genuine admissible violation to counterexample construction for full verification.
- Hand off an extremal family requiring an explicit object to object construction.
- Hand off a plausible constant or rate formula to bound and rate refinement.
- If all tests succeed, say only that no violation was found in the tested family.
- If a test violates an assumption, label it diagnostic rather than a counterexample.
- If finite search is exhausted, do not infer necessity, truth, or optimality.
- A near-equality example suggests a candidate constant but does not establish sharpness.
- Keep code output and scratch tables outside the final proof unless rederived symbolically.
- State which tested cases actually respect every target assumption.
- Final positive or negative conclusions require the ordinary complete-proof verification chain.
