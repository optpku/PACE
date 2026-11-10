# Decompose and Plan Route

# Input Contract

- Read the active problem and immutable target contract before proposing a route.
- Read the proof demand, exact target scope, and authorized assumptions embedded in the active problem dossier.
- Read the selected mathematical routes, including exact source statements, hypotheses, ordered construction steps, authority, applicability notes, and open bridges.
- Read independently verified relations, proof-bound derivation material, rejected relations, do-not-repeat guidance, and the fresh blocker from verified progress when present.
- Read exact verified dependency relations with their identities, scopes, and authorized uses.
- Read the algorithm success contract when the target requires an algorithm.
- Treat every Plan claim as a future proof obligation, never as an authorized premise.
- Keep the target's quantifiers, class, oracle model, resources, and conclusion unchanged.
- Classify whether the endpoint is universal, existential, positive, negative, or conditional.
- Use only the artifacts exposed for this invocation; do not search sibling runs.

# Procedure

1. Restate the exact endpoint and list the clauses that the final proof must assemble.
2. Normalize the mathematical objects, quantifiers, parameter ranges, and resource model.
3. Keep proof and refutation branches distinct and give each the logically correct endpoint.
4. Work backward from each target clause to the last relation that would imply it.
5. Work forward from authorized premises and verified reusable relations.
6. Mark a verified dependency as reused and do not re-prove it inside an unrelated child route.
7. Identify the smallest missing bridge between the forward and backward chains.
8. Test a proposed bridge on a legal specialization; if it becomes equivalent to the target, keep it as the unresolved target-critical step rather than claiming progress.
9. Require the bridge to be independently derivable from upstream relations, not merely a target-equivalent recovery statement with a new name.
10. Choose one coherent route rather than enumerating unrelated proof strategies.
11. Give each step one quantitative claim with explicit mathematical content.
12. Record dependencies so every step consumes only earlier steps or authorized premises.
13. Mark the target-critical step and explain why it is load bearing.
14. State why that step is plausible under the exact active assumptions.
15. Put only the step's remaining internal derivation in its Prover obligation; never hide the whole route or an unnamed new theorem there.
16. Give a falsification test under every premise of the exact bridge; a counterexample to a weakened lemma does not falsify its strengthened replacement.
17. For topology or set-valued routes, specify the convergence topology, required boundedness, and exact graph-membership recovery before relying on closure.
18. For a selected source mechanism, identify its exact insertion role in the route.
19. Bind each selected ordered construction step to the dependent Plan relation that uses it.
20. Attach every recorded source condition and constant dependency to the exact step where it is needed.
21. Record every extra source hypothesis as a bridge to prove, not an added assumption.
22. For constructions, expose the supplied construction steps and separate object definition from verification of each required property.
23. For an iterative or recursive route, expose the needed object and update definitions, exact one-step relation, invariant or continuation argument, and terminal recurrence, rate, or assembly as checkable dependent claims.
   Put region-containment and oracle-legality claims before any local smoothness/model claim that
   consumes them. Mark the first relation that establishes each new point is admissible; a route
   that uses a later Lyapunov inequality to justify an earlier model estimate is circular and must
   be revised into a bootstrap or virtual-step argument.
   For adaptive search, add a separate claim for rejected-trial legality and another for the
   disjoint potential/charging bound; do not hide both in a single “overhead” sentence.
24. Omit parts of that pattern that the mathematics does not need, but never replace its missing load-bearing relations by a single newly named theorem equivalent to the target.
25. For algorithms, cover every nonempty success-contract requirement without inventing others.
26. Include terminal specialization, constants, cases, and complexity assembly where required.
27. Preserve verifier-backed parts of a previous route and replace only the refuted ingredient.
28. On Plan revision, split any verifier-identified opaque blocker into explicit local relations before handing it back to the Prover.
29. Ensure the route contains a path from every key step to the final target.
30. Produce one canonical Plan with executable steps and explicitly unresolved research obligations.

# Handoff and Failure Discipline

- Use the Host's current proof-state balance: each Research or SMI dispatch
  consumes one, including partial or failed attempts. SMI subphases share that
  reservation. Prover assembly and repair, other roles and fixed verification
  or reporting consume no proof-states. At zero balance, rely on existing
  evidence and preserve unresolved gaps; round, call and time limits still apply.
- Explain the evidence supporting the next bounded node, the available way to
  attempt it, and the mechanism that remains unknown. A precise formula or route
  node is not by itself evidence that its mechanism is ready for execution.
- Recommend lightweight Research for a bounded local attempt, or deeper,
  longer SMI research when conjecture formation, mechanism discovery, comparison
  of substantial alternatives, or a nontrivial construction is the main work.
  Existing exact nodes can need SMI. Give the mathematical reason in the ordinary
  handoff; Director owns dispatch and Planner retains the canonical route.
- Recommend at most two eligible workflow Skills as an advisory starting point.
- Bind the recommendation objective to the key step, construction, or route-critical bridge.
- Do not place Skill ids in premise bindings, mathematical claims, or dependency facts.
- If a source mechanism does not apply, expose the exact mismatch or plan a lawful replacement.
- If a previous mechanism was refuted, require a genuinely new mathematical ingredient.
- If the target needs a missing construction, hand off to the appropriate construction workflow.
- If constants or rates remain uncertain, state the exact local relation and include only its precise remaining derivation and stress test in the obligation.
- Do not re-prove a verified sibling lower bound merely to mention sharpness compatibility.
- If no route closes, return the strongest honest Plan with explicit unresolved obligations.
- Do not use the target itself, or an equivalent reformulation at the same quantifier scope, as an auxiliary lemma.
- Never convert lack of a route into a theorem-level negative conclusion.
- The Plan guides search; the final proof and fresh verification retain mathematical authority.

## Further route guidance

# Method card

**Do not claim:** a route is valid because it is plausible, named, or target-equivalent.

Start from the literal clauses and quantifiers. Build the forward and backward dependency chains, isolate one independently derivable bridge, and attach conditions and falsifiers at the step where they are used. If the bridge is target-equivalent or a route premise is refuted, return a route revision instead of relabeling the gap.

First separate branches whose hypotheses or endpoints differ. For each hard node,
propose an operation, the relation it should produce and the exact use made by
its consumer. Explore a change of representation when the current variables
hide cancellation or a controllable quantity. Prefer the weakest sufficient
bridge; a uniform bound or full construction may exceed what the target needs.
Separate what the proposed mechanism already explains from the ingredient still
needing discovery. A precise relation with no supported way to obtain it can
be a good SMI research question rather than a ready local derivation.

**Handoff:** return one scoped bridge, its dependencies and falsifier, or the exact route revision required.
