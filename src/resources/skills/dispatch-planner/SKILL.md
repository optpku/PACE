---
name: dispatch-planner
description: Choose Planner to create, revise, or rewrite the mathematical route.
---

# Calling planner

Read the [common dispatch guidance](../director-dispatch/SKILL.md)
together with these role-specific instructions.

Call `planner` only when no valid route exists, or when a specific committed
result invalidates or materially changes the current route. An incomplete node
or local gap does not by itself require replanning; dispatch bounded work on the
existing route instead.

For `planner`, begin with CREATE_ROUTE, REVISE_ROUTE, or REWRITE_ROUTE.
Use CREATE_ROUTE when no usable route exists. Use REVISE_ROUTE when a committed
result changes a bounded part of the existing route; provide its exact current
reference and the evidence motivating the change. Use REWRITE_ROUTE when the
main approach is invalidated or the impact cannot be kept local. Ask the Planner
to determine the mathematical changes and their downstream effects. Do not
prescribe the replacement lemmas, dependencies or proof order.
