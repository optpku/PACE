# Research Framing Representation Repair

Use this instruction only when the Composer prompt seed names both a prior
candidate ref and representation diagnostics ref. Read those exact files along
with the original problem, target contract, and Research Framing role prompt.

Return one complete replacement research brief. The outer object must retain a
`work_items` list for the research loop; other top-level summaries are
optional. Repair the reported
representation problem while preserving unaffected mathematical statements as
closely as possible. A schedulable item needs a unique `work_item_id`; other
research fields are model-owned and may remain sparse, be omitted when the
context is unavailable, or include additional explanation when useful. Prefer
the supplied target, assumption, and input identities, but do not turn their
absence into a schema failure or invent a replacement. Do not add a Host
status, completion binding, route ID, mechanism ID, or verdict.

Return JSON only.
