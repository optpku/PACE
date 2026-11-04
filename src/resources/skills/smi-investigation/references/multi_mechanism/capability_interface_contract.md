# Capability Interface Protocol

Treat a mechanism interface as an explicit mathematical promise between
producer and consumer. Describe each capability with an ID, exact statement,
kind, mathematical type, domain, quantifiers, epistemic status, and direct
source or mechanism refs.

## Identity and dependency

Compare the tuple `(id, statement, kind, mathematical_type, domain, quantifiers)`
directly. Equal IDs with unequal tuple fields do not match. A nonprimitive input
must name an earlier accepted mechanism and the exact output it consumes.
Proposal order alone does not establish a dependency.

A primitive must be available in the frozen anchor. An unproved target assertion
is never an available input or established output.
Accepted mechanism outputs remain reviewed search support, not proof premises.

Reject circular use of a mechanism's own output, a synonym for that output, or
the candidate target. Reject an output stronger than the
shown mathematical move. An empty dependency list is valid only when the move
really follows from anchor assumptions alone.
