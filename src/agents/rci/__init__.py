"""Agent-owned multi-role RCI research capsule."""

from .phase_runner import RciPhaseError, RciPhaseResult, RciPhaseRunner
from .pipeline import RciPipeline
from .roles import ROLE_BINDINGS, RciRoleBinding, role_binding

__all__ = [
    "ROLE_BINDINGS",
    "RciPhaseError",
    "RciPhaseResult",
    "RciPhaseRunner",
    "RciPipeline",
    "RciRoleBinding",
    "role_binding",
]
