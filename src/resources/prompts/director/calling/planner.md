---
name: dispatch-planner
description: Choose Planner to create, revise, or rewrite the mathematical route.
---

# Calling planner

Read the [common dispatch guidance](../dispatch.md)
together with these role-specific instructions.

Call `planner` only when no valid route exists, or when a specific committed
result invalidates or materially changes the current route. An incomplete node
or local gap does not by itself require replanning; dispatch bounded work on the
existing route instead.

Use Planner when the requested work needs a mathematical route. A source-only
review or report does not need a proof route simply because none exists. Retain
mathematical planning when the request also requires a new result, independent
proof, computation or reproduction.

For `planner`, begin with CREATE_ROUTE, REVISE_ROUTE, or REWRITE_ROUTE.
Use CREATE_ROUTE when no usable route exists. Use REVISE_ROUTE when a committed
result changes a bounded part of the existing route; provide its exact current
reference and the evidence motivating the change. Use REWRITE_ROUTE when the
main approach is invalidated or the impact cannot be kept local. Ask the Planner
to determine the mathematical changes and their downstream effects. Do not
prescribe the replacement lemmas, dependencies or proof order.

A calculation may justify reconsidering a dependency before the replacement
has been proved. Provide the frozen route and the original calculation. Ask
Planner to identify the affected use and the remaining proof obligations.


## Versioned task continuity

For each load-bearing obligation, preserve its stable identity, exact scope,
parent objective, and intended consumer. Mark whether it is required by the
original target or is a sufficient condition of this particular route.

Keep a proved result separate from its proposed application. Record the missing
application argument and the conditions that must hold at the consumer. Neither
a completed task nor a checked premise alone closes that application.

Use the supplied frozen route as the revision base. Update only affected
obligations and consumers. Keep detailed proofs in their original manuscripts;
the route states the dependencies, unresolved implications, and next bounded
work item. A route rewrite requires a concrete reason that local revision is
insufficient. Never rename an unchanged gap as a newly solved route node.

Maintain a single current work order. At a revision replace the superseded handoff. Name the dependency that changed, the exact version and section supporting the change, the next mathematical task and the condition for returning to the Director. Keep live obligation anchors and conditions. Cite earlier routes for history and original manuscripts for derivations. When an application lacks a bridge, specify what must be shown at the consumer.
