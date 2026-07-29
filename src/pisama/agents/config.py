"""Configuration management for Agent SDK integration."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .chaos.config import ChaosConfig


@dataclass
class BridgeConfig:
    """Configuration for the detection bridge.

    Can be loaded from:
    - Environment variables (PISAMA_*)
    - JSON config file
    - Direct instantiation

    Example:
        # From environment
        config = BridgeConfig.from_env()

        # From file
        config = BridgeConfig.from_file(Path("~/.pisama/config.json"))

        # Direct
        config = BridgeConfig(warning_threshold=30, block_threshold=50)
    """

    # Detection thresholds
    warning_threshold: int = 40  # Severity to trigger warnings
    block_threshold: int = 60  # Severity to trigger blocking

    # Timeout settings (in milliseconds)
    detection_timeout_ms: float = 80  # Max time for detection (leave 20ms buffer)
    total_timeout_ms: float = 100  # Hard limit for hook execution

    # Feature flags
    enable_blocking: bool = True  # Allow blocking tool calls
    enable_recovery: bool = True  # Enable PostToolUse recovery messages
    fail_open: bool = True  # Allow execution on detection errors

    # Session config
    context_window: int = 10  # Recent spans to include in context
    max_sessions: int = 100  # Maximum concurrent sessions
    session_ttl_seconds: int = 3600  # Session expiry time

    # Detector filtering
    enabled_detectors: list[str] = field(
        default_factory=lambda: ["loop", "repetition", "coordination", "cost"]
    )
    disabled_detectors: list[str] = field(default_factory=list)

    # Tool patterns (regex)
    tool_patterns: list[str] = field(default_factory=lambda: [".*"])
    excluded_tools: list[str] = field(
        default_factory=lambda: ["AskUserQuestion", "user_input"]
    )

    # Logging
    log_level: str = "INFO"
    log_detections: bool = True

    # Chaos engineering (optional, off by default)
    chaos: Optional["ChaosConfig"] = None

    # Telemetry (opt-in, off by default)
    enable_telemetry: bool = False
    telemetry_api_key: str = ""
    telemetry_host: str = "https://us.i.posthog.com"

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        """Create config from environment variables.

        Environment variables:
            PISAMA_WARNING_THRESHOLD: Warning severity threshold
            PISAMA_BLOCK_THRESHOLD: Blocking severity threshold
            PISAMA_TIMEOUT_MS: Detection timeout
            PISAMA_ENABLE_BLOCKING: Enable/disable blocking
            PISAMA_ENABLE_RECOVERY: Enable/disable recovery messages
            PISAMA_CONTEXT_WINDOW: Context window size
            PISAMA_LOG_LEVEL: Logging level
        """
        return cls(
            warning_threshold=int(os.getenv("PISAMA_WARNING_THRESHOLD", "40")),
            block_threshold=int(os.getenv("PISAMA_BLOCK_THRESHOLD", "60")),
            detection_timeout_ms=float(os.getenv("PISAMA_TIMEOUT_MS", "80")),
            enable_blocking=os.getenv("PISAMA_ENABLE_BLOCKING", "true").lower()
            == "true",
            enable_recovery=os.getenv("PISAMA_ENABLE_RECOVERY", "true").lower()
            == "true",
            fail_open=os.getenv("PISAMA_FAIL_OPEN", "true").lower() == "true",
            context_window=int(os.getenv("PISAMA_CONTEXT_WINDOW", "10")),
            log_level=os.getenv("PISAMA_LOG_LEVEL", "INFO"),
            enable_telemetry=os.getenv("PISAMA_TELEMETRY", "false").lower() == "true",
            telemetry_api_key=os.getenv("PISAMA_TELEMETRY_API_KEY", ""),
            telemetry_host=os.getenv("PISAMA_TELEMETRY_HOST", "https://us.i.posthog.com"),
        )

    @classmethod
    def from_file(cls, path: Path) -> "BridgeConfig":
        """Create config from JSON file.

        Expected format:
            {
                "detection": {
                    "warning_threshold": 40,
                    "block_threshold": 60,
                    "timeout_ms": 80
                },
                "session": {
                    "context_window": 10,
                    "max_sessions": 100
                },
                "logging": {
                    "level": "INFO"
                }
            }
        """
        with open(path) as f:
            data = json.load(f)

        # ``save()`` writes the dataclass's flat public shape. Older config
        # files used nested detection/session/logging sections, so accept both
        # formats and keep save/from_file a true round trip.
        if not any(section in data for section in ("detection", "session", "logging")):
            allowed = {item.name for item in fields(cls)}
            return cls(**{key: value for key, value in data.items() if key in allowed})

        # Handle nested structure
        detection = data.get("detection", {})
        session = data.get("session", {})
        logging_cfg = data.get("logging", {})

        return cls(
            warning_threshold=detection.get("warning_threshold", 40),
            block_threshold=detection.get("block_threshold", 60),
            detection_timeout_ms=detection.get("timeout_ms", 80),
            enable_blocking=detection.get("enable_blocking", True),
            enable_recovery=detection.get("enable_recovery", True),
            fail_open=detection.get("fail_open", True),
            enabled_detectors=detection.get("enabled_detectors", []),
            disabled_detectors=detection.get("disabled_detectors", []),
            context_window=session.get("context_window", 10),
            max_sessions=session.get("max_sessions", 100),
            session_ttl_seconds=session.get("ttl_seconds", 3600),
            tool_patterns=data.get("tool_patterns", [".*"]),
            excluded_tools=data.get("excluded_tools", []),
            log_level=logging_cfg.get("level", "INFO"),
            log_detections=logging_cfg.get("log_detections", True),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)

    def save(self, path: Path) -> None:
        """Save config to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


def load_config(
    config_path: Optional[Path] = None,
    use_env: bool = True,
) -> BridgeConfig:
    """Load configuration with fallback chain.

    Priority:
    1. Explicit config_path
    2. Environment variable PISAMA_CONFIG_PATH
    3. Default ~/.pisama/agent_sdk_config.json
    4. Environment variables (PISAMA_*)
    5. Default values

    Args:
        config_path: Explicit path to config file
        use_env: Whether to fall back to environment variables

    Returns:
        BridgeConfig instance
    """
    # Check explicit path
    if config_path and config_path.exists():
        return BridgeConfig.from_file(config_path)

    # Check env var for path
    env_path = os.getenv("PISAMA_CONFIG_PATH")
    if env_path:
        path = Path(env_path)
        if path.exists():
            return BridgeConfig.from_file(path)

    # Check default location
    default_path = Path.home() / ".pisama" / "agent_sdk_config.json"
    if default_path.exists():
        return BridgeConfig.from_file(default_path)

    # Fall back to env vars
    if use_env:
        return BridgeConfig.from_env()

    # Default config
    return BridgeConfig()
