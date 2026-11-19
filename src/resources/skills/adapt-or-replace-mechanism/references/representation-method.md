# Change the representation

Use this method when the assigned relation is obstructed by an uncontrolled
offset, coupled terms, unsuitable variables or an inconvenient object class.
An external source is optional. The transformation must preserve the assigned
statement; a scope change is a proposal for Planner.

Start with the consumer's exact needed inequality or construction and identify
the term that the present representation cannot control. Choose a transformation
for that obstacle: shift an offset, introduce an auxiliary variable to cancel
terms, separate coupled components, rescale parameters or compare primal and
dual representations. Give its formulas and derive the resulting relation.

Then check what the transformation preserves: objective values or gaps,
solutions, domain membership, quantifier order and parameter dependence. If
only a one-way implication holds, state that direction and its recovery
requirements. Do not call a relaxation an equivalence.

For an algorithmic target, also derive how the transformed operations are
implemented by the original oracle. A quantity such as an optimizer may be
used in analysis without being available to the algorithm only when it cancels
from every actual update. Write the cancellation explicitly. For example,
moving a linear term between two components preserves their sum, but its
gradient and proximal shifts still need to cancel in the executed operation.

Track the exact object selected by optimality, duality or another operation.
Its membership does not transfer a norm bound on a different existential
witness. A universal bound may apply after proving the selected membership;
otherwise derive the needed bound independently or expose the unknown parameter.

If the transform leaves the original obstacle intact, record that failed
equation and propose a different ingredient. If it introduces a new assumption,
leave its applicability open instead of calling the target solved.

Research can try fresh local representations within its bounded
assignment. If finding the representation itself requires deeper comparison of
substantial alternatives or a new mechanism, return the attempts and remaining
obstacle to Director with a reasoned RCI recommendation. A precise assigned
relation does not exclude this deeper research.

**Handoff:** give the transformation, preservation/recovery derivation,
implementability where required, the newly accessible relation and the remaining
condition. An unverified transformation remains reconstruction context.
