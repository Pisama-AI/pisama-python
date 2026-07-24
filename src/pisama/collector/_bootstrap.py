"""Bootstrap script for pisama watch.

Set as PYTHONSTARTUP to auto-instrument the target subprocess.
The pisama-auto package must be installed for instrumentation to work.
If not installed, the subprocess runs normally without instrumentation.
"""

try:
    import pisama_auto

    pisama_auto.init(auto_patch=True)
except ImportError:
    pass  # pisama-auto not installed, skip silently
except Exception:
    pass  # Any other error, don't break the user's process
