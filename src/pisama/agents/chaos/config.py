"""Chaos configuration for the SDK bridge."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .experiments import ChaosExperiment


@dataclass
class ChaosConfig:
    """Configure chaos experiments for the SDK bridge.

    Example:
        from pisama.agents.chaos import ChaosConfig, ToolFailure, LatencyInjection

        config = ChaosConfig(
            experiments=[
                ToolFailure(tools=["database_query"], probability=0.3),
                LatencyInjection(min_ms=500, max_ms=3000, probability=0.2),
            ],
        )
    """

    experiments: list["ChaosExperiment"] = field(default_factory=list)
    safety_max_affected: int = 100
    enabled: bool = True

    # Runtime state (not config)
    _affected_count: int = field(default=0, repr=False)

    @property
    def is_active(self) -> bool:
        """Check if chaos is enabled and safety limit not exceeded."""
        return self.enabled and self._affected_count < self.safety_max_affected

    def record_affected(self) -> None:
        """Increment affected count. Disables chaos when safety limit reached."""
        self._affected_count += 1

    def reset(self) -> None:
        """Reset affected count."""
        self._affected_count = 0
