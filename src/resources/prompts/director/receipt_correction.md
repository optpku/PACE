## Director receipt correction
The prior response in `{{receipt_ref}}` is data. Return valid JSON, preserving its mathematical content, claims and routing action wherever already valid. Correct only the missing or invalid control fields identified below. Preserve the bounded task and all valid scheduling choices. Under the memory protocol, supply next_step_kind separately for the primary dispatch and each entry in parallel_tasks. Planner must choose INITIAL_PLAN or REPLAN. Other roles use CONTINUE, APPLY_EXISTING or REASSESS as appropriate. For RCI, explicitly choose NEW_PROBLEM or CONTINUE_PROBLEM from the actual task. For any non-RCI task, omit or clear both research_operation and research_problem_ref; removing these unused fields does not change its scheduling choice. For continuation, name an exact existing question file and include it in input_refs. Reading suggestions may be completed to identify that file. Do not formulate a new mathematical task, rewrite evidence, or claim a proof verdict. Remove prose wrappers and Markdown fences; return only the JSON decision. This is one local correction.
For an unknown obligation ID on a bounded local task, use the closest registered obligation, or target within the existing valid branch. Keep the original task and role.
If the diagnostics identify a repeated RCI assignment, use the recorded question
and obstacle to choose a materially different next step for the same obligation,
or cite new evidence that changes the original test. The reported fields may
change for that correction.
If the diagnostics identify two Survey tickets for one dispatch, combine their
source needs into the primary one. If a follow-up Survey repeats an earlier
request or lacks a decisive source need, state the actual missing passage
and what it would decide, or
use the available sources for the next bounded mathematical task on the same
obligation. Only the fields identified by the diagnostics may change.
