# Checking a Draft Against a PACE Finding

The cases below are invented editing exercises. They are not findings from
a PACE run and must not be inserted as results into the current manuscript.

| Source information | Draft defect | Correction to the description |
| --- | --- | --- |
| A residual bound holds for every starting point in a specified set \(D\). | The abstract promises convergence from every starting point. | Retain \(D\) and the residual bound actually proved; do not substitute a convergence claim. |
| For each fixed step size \(h\), the proof supplies a threshold \(K(h)\). | The summary says that one threshold works for all admissible step sizes. | Keep the threshold dependent on \(h\), unless the source also supplies a uniform statement. |
| The proof bounds \(\min_{0\leq j<N} r_j\). | The paper assigns the same bound to \(r_{N-1}\). | Describe the best residual among the first \(N\) terms; a last-term estimate is a separate assertion. |
| A construction uses \(2^d\) objects but gives no bound on an algorithm's execution. | The report calls \(2^d\) an iteration-complexity bound. | Report the size of the construction and leave algorithmic cost undecided. |
| An example violates the proposed conclusion but fails one required regularity assumption. | The report presents it as a counterexample under all the original hypotheses. | Identify the failed assumption and the scope actually illustrated by the example. |
| A supplied proof gives \(\inf_k r_k\geq\delta>0\). | The opening only says that the residual may fail to vanish. | Preserve the proved positive margin and the parameter range on which it holds. |
| A recorded enclosure certifies a finite interval computation. | The paper describes an infinite trajectory as certified. | Give the finite scope, arithmetic guarantee, and source of the recorded check. |
| A conditional procedure uses a finite certificate instead of an unknown global constant. | The summary adds that global constant as a required input. | Retain the procedure's actual input and certificate conditions. |

For an actual discrepancy, inspect the formal statement and relevant proof,
then locate every retelling in the draft. Correct those passages together.
If the needed justification is already in the source, restore it. If it is
absent, explain the gap affecting that conclusion without starting a new
proof or experiment.

Compare an unresolved question with the findings already available. A
counterexample to a claim on a large class does not decide every subclass;
conversely, that refuted claim should not still appear as an open conjecture
at the same scope. Check any assertion about the literature against the
acquired publication, with its actual theorem or section locator.

Record required changes in the assigned review or revision response. The
writer can repair exposition; only the mathematical workflow can establish
a new result or change the committed verdict.
