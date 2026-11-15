"""Agent-owned multi-role SMI research capsule."""

from .phase_runner import SmiPhaseError, SmiPhaseResult, SmiPhaseRunner
from .pipeline import SmiPipeline, SmiPipelineProtocolError
from .roles import ROLE_BINDINGS, SmiRoleBinding, role_binding

__all__ = [
    "ROLE_BINDINGS",
    "SmiPhaseError",
    "SmiPhaseResult",
    "SmiPhaseRunner",
    "SmiPipeline",
    "SmiPipelineProtocolError",
    "SmiRoleBinding",
    "role_binding",
]
