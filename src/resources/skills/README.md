# Mathematical method library

Choose a method for the current obligation, then read its `SKILL.md` and only
the relevant references or example. Links below are relative to this index.
Director may recommend a method in `prompt_brief` or name its file in
`input_refs`; the Host does not select mathematical strategies.

| Skill | Use when | Typical role |
| --- | --- | --- |
| [Survey and source analysis](survey-and-source-analysis/SKILL.md) | Check relevant existing results and acquire the source passages needed to assess their coverage. | Survey |
| [Evidence reuse](evidence-reuse/SKILL.md) | Carry an earlier result into a changed candidate or condition profile. | Planner, Research, Prover |
| [SMI investigation](smi-investigation/SKILL.md) | Develop conjectures and mechanisms through support, coverage, and challenge. | Director, SMI |
| [Failure diagnosis](failure-diagnosis/SKILL.md) | Turn a failed attempt or review into a precise next obligation. | Planner, Research, Prover |
| [Route planning](decompose-and-plan-route/SKILL.md) | Connect premises to the target through explicit dependencies. | Planner |
| [Direct proving](direct-proving-and-gap-closure/SKILL.md) | Derive a scoped claim or repair the load-bearing gap. | Research, Prover |
| [Mechanism adaptation](adapt-or-replace-mechanism/SKILL.md) | Transfer a source argument or replace its failed ingredient. | Planner, Research, Prover |
| [Object construction](construct-object-under-properties/SKILL.md) | Build an admissible object and prove its needed properties. | Research, Prover |
| [Algorithm construction](construct-algorithm-under-contract/SKILL.md) | Define legal operations and prove their guarantees and cost. | Research, Prover |
| [Counterexamples](construct-counterexamples/SKILL.md) | Produce an admissible witness at the exact challenged scope. | Research, Prover |
| [Toy and boundary cases](explore-toy-and-boundary-cases/SKILL.md) | Diagnose signs, endpoints, scaling, or suspected obstructions. | Research, Prover |
| [Bounds and rates](refine-bounds-rates-and-constants/SKILL.md) | Solve a recurrence and derive the requested error or resource bound. | Research, Prover |
| [Mathematical exposition](mathematical-exposition/SKILL.md) | Explain established results and their mechanism. | Writer, Editor |
| [Claim preservation](mathematical-claim-preservation/SKILL.md) | Check a draft against the exact source claims and conditions. | Writer, Editor |
| [Paper composition](theory-paper-composition/SKILL.md) | Organize supplied mathematics into the assigned paper. | Writer, Editor |

Role labels are reading suggestions, not new agents. External retrieval follows
the Host's setting and the assigned role's instructions. Survey acquires missing
external sources during mathematical research; other mathematical roles use
acquired material and request Survey for additional source work. Manuscript
roles follow their writing task's limits on citation checks. Preserve exact URLs,
versions and locators. ReasAtlas, when separately configured, is a source
retrieval option; it is not bundled here.

Both prompt variants use this library. Skills and their illustrative examples
remain search context. They cannot change the target, certify novelty, or
grant a mathematical PASS. SMI phases receive their own method files; blind
probes retain their statement-only task boundary. Auditor/Kernel use independent
verification prompts, and manuscript roles keep their assigned writing guides.
