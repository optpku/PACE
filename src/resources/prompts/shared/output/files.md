# Task files

Write the assigned mathematical deliverable and its named supporting files.
Keep all supplied originals read-only and preserve their exact assumptions and
verification scope. Save complete reasoning in the deliverable rather than
returning only a completion receipt. Return the assigned path when finished.

Structured `*_ref` fields contain one exact workspace-relative file path,
without backticks, section locators, explanations or additional paths. Put
locators and explanations in prose fields instead. `read_file` and `search_file`
take file paths; use `list_files` to discover permitted files in a directory.
Route obligations may use separate `statement_locator` and `scope_locator`
fields containing `#existing-anchor`; their `statement_ref` and `scope_ref`
remain plain file paths. Never edit a manuscript to fix a control-file reference.
