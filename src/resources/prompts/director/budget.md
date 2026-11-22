

## Current Host budget and STOP contract
Remaining shared proof states: {{proof_states}}
Remaining research rounds (including the next dispatch): {{rounds}}
Research and RCI dispatches each spend one proof state; all specialist dispatches count toward a separate round cap when set. No separate cap does not mean zero rounds. These live counters take precedence over historical artifacts and general role guidance. A limit of one user turn does not limit dispatches within that turn. STOP requires stop_reason and a concrete rationale: use BUDGET_EXHAUSTED only when a current Host counter is zero and no eligible task can advance the request; use NO_ACTIONABLE_TASK only when there is no remaining useful task, stating the concrete blocker or completed coverage. While an actionable gap and budget remain, DISPATCH one bounded task. A rejected manuscript with an independently supported core can be narrowed and rechecked without redoing the original research. Omit stop_reason for DISPATCH.
