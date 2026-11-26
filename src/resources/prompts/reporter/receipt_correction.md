## JSON receipt correction
Repair the saved Reporter receipt in `{{receipt_ref}}`. Return the complete receipt. Fix references and fill only missing or invalid fields. Preserve any valid original summary, outcome and application identities. Ground missing content in the supplied receipt and diagnostics. For a reference choose one exact path from the schema, without a section or annotation. Retain the assigned branch {{branch_id}} and obligation IDs {{obligation_ids}}. {{proof_constraint}}Do not rereview the mathematics, invent a premise, or rewrite manuscripts.
Omit any proposal that has neither a usable identity nor an assigned obligation. Keep identifiable proposals in order and repair their control fields. If no application remains, use an outcome other than APPLICATION_PROPOSED.
{{diagnostics}}
Original content to preserve (except the invalid file references):
{{original_payload}}
