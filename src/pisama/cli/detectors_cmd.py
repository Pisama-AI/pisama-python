"""pisama detectors command."""

from __future__ import annotations

import click

from pisama.output.terminal import display_detector_list


@click.command("detectors")
def detectors_cmd() -> None:
    """List all registered failure detectors."""
    # Importing the detectors package triggers auto-registration
    from pisama_core.detection.detectors import __all__ as _loaded  # noqa: F401
    from pisama_core.detection.registry import registry

    detectors = registry.get_all()

    if not detectors:
        click.echo("No detectors registered.")
        return

    display_detector_list(detectors)
