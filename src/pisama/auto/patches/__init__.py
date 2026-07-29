"""Auto-patching for LLM libraries.

Detects installed libraries and patches them to emit OTEL spans
with gen_ai.* semantic conventions.
"""

import importlib
import logging
from typing import List

logger = logging.getLogger("pisama.auto")

# Map of library name -> patch module
_PATCHABLE = {
    "anthropic": ".anthropic_patch",
    "openai": ".openai_patch",
}

_patched: List[str] = []


def patch_all() -> List[str]:
    """Patch all detected LLM libraries.

    Returns:
        List of library names that were successfully patched.
    """
    for lib_name, patch_module in _PATCHABLE.items():
        if lib_name in _patched:
            continue
        try:
            importlib.import_module(lib_name)
        except ImportError:
            continue

        try:
            mod = importlib.import_module(patch_module, package="pisama.auto.patches")
            mod.patch()
            _patched.append(lib_name)
            logger.debug(f"Patched {lib_name}")
        except Exception as e:
            logger.warning(f"Failed to patch {lib_name}: {e}")

    return list(_patched)


def patch(library: str) -> bool:
    """Patch a specific library.

    Args:
        library: Library name (anthropic, openai)

    Returns:
        True if patched successfully
    """
    if library in _patched:
        return True

    if library not in _PATCHABLE:
        logger.warning(f"Unknown library: {library}. Supported: {list(_PATCHABLE.keys())}")
        return False

    try:
        mod = importlib.import_module(_PATCHABLE[library], package="pisama.auto.patches")
        mod.patch()
        _patched.append(library)
        return True
    except Exception as e:
        logger.warning(f"Failed to patch {library}: {e}")
        return False
