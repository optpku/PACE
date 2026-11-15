---
name: dispatch-prover
description: Assign Prover to assemble or repair one complete candidate answering the literal mathematical target from applicable evidence and independent reviews.
---

# Call Prover

Use Prover when the available evidence can support complete literal-target
assembly, or an independent review identifies a bounded repair of that candidate.
Provide the exact target, applicable source arguments, checked findings, needed
dependencies and the review requiring repair. Expect a self-contained candidate
preserving the original assumptions, quantifiers and requested scope.

Set `agent_id` to `prover` and begin `prompt_brief` with `ASSEMBLE_CANDIDATE`.
An isolated unproved route node belongs to Research or RCI. When the user permits
a scoped partial research answer, assembly may present established results and
precise open boundaries; otherwise preserve the whole target. Formatting existing
material into a report is a Host writing task, not proof assembly.

Prover reserves no proof-state and can assemble or repair from existing evidence
after Research/RCI credits run out. Its task still consumes one research round.
Fresh Auditor/Kernel checks decide acceptance; neither the Director nor Prover
can turn source coverage, memory or a local finding into a final verdict.

Example, when the current findings cover the complete requested target:

```json
{"action":"DISPATCH","agent_id":"prover","prompt_brief":"ASSEMBLE_CANDIDATE: Assemble the complete answer to the literal target from the checked findings. State all conditions, justify each required implication and preserve the requested scope.","input_refs":["work/turn001/input/problem.md","results/findings/README.md"],"rationale":"已有成果覆盖目标，下一步应整合完整候选并接受独立核验。"}
```
