
Read the manuscript once and review all entries in applications together. For
each entry, check its own bound premises, hypotheses, quantifiers and connecting
argument. Keep the scopes separate: another application's premises do not
silently establish this one. Return application_checks with exactly one entry
per requested application_key, including that key, obligation_id,
obligation_version, status (APPLICABLE, NOT_ESTABLISHED, or VERIFIER_ERROR for an unfinished check), and the shared
review_ref. Write one detailed review to that review_ref with a separate
explanation for each scope. A failed application does not invalidate a valid
manuscript or another valid application. For each application explicitly set its own reuse_authorized flag, true only
for APPLICABLE. Assess that use under its own premises even when another
claim in the manuscript fails. An unfinished check grants no authority.

When publication_requested is true, decide CREATE, UPDATE or NONE once for the
complete manuscript. Otherwise return publication.action=NONE; this call checks
applications and does not reconsider the findings catalog. If an earlier finding
review is supplied, retain its checked statement and assumptions, then examine
the new applications. A prior PASS never establishes a new application by itself.
Do not create a new manuscript or new mathematics during verification.
