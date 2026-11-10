"""Agent-owned multi-role RCI research capsule."""

from .phase_runner import RciPhaseError, RciPhaseResult, RciPhaseRunner
from .pipeline import RciPipeline, RciPipelineProtocolError
from .roles import ROLE_BINDINGS, RciRoleBinding, role_binding

__all__ = [
    "ROLE_BINDINGS",
    "RciPhaseError",
    "RciPhaseResult",
    "RciPhaseRunner",
    "RciPipeline",
    "RciPipelineProtocolError",
    "RciRoleBinding",
    "role_binding",
]
