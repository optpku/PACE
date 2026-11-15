---
name: dispatch-survey
description: Assign literature investigation, source comparison, acquisition or applicability checks to Survey, including review-only user requests.
---

# Call Survey

Use Survey to establish what existing sources actually support: assumptions,
quantifiers, proof coverage, comparisons and remaining source gaps. A literature
review can be the complete requested outcome. Missing primary text that affects
the next research decision is also a reason to call Survey.

Provide the user's questions, acquired relevant material and the specific source
or comparison gap. Reuse existing source indexes instead of repeating covered
acquisition. Only Survey may retrieve external sources, when the Host enables it.
When retrieval is unavailable, assign useful work on supplied material or report
the limitation. A source gap alone does not create an original-proof obligation.

Set `agent_id` to `survey`. Give a clear investigation question and request an
evidence report with exact source locations, conditions and uncertainties. Survey
neither redesigns a mathematical route nor accepts a new mathematical claim.

Example, when the two supplied notes have not yet been compared:

```json
{"action":"DISPATCH","agent_id":"survey","prompt_brief":"Compare the supplied notes' assumptions and conclusions. Deliver a sourced comparison and remaining coverage gaps; the user requests a review of existing material.","input_refs":["sources/note_a.md","sources/note_b.md"],"rationale":"已有原文，但所需比较尚未完成。"}
```

When the requested coverage is already supported, end research and let the Host
handle separately requested report writing. For a mixed request, retain its
remaining original-research or proof obligations.
