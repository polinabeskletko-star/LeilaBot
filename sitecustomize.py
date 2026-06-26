"""Runtime customisation for LeilaBot.

Python imports sitecustomize automatically on startup when it is available on
sys.path. This lets us install small runtime patches without rewriting the
large bot.py file.
"""

import sys


class _ModuleProxy:
    def __init__(self, globals_dict):
        object.__setattr__(self, "_globals", globals_dict)

    def __getattr__(self, name):
        return self._globals[name]

    def __setattr__(self, name, value):
        self._globals[name] = value


def _trace_calls(frame, event, arg):
    if event != "call":
        return _trace_calls

    if frame.f_code.co_name != "main":
        return _trace_calls

    filename = frame.f_code.co_filename.replace("\\", "/")
    if not filename.endswith("/bot.py") and not filename.endswith("bot.py"):
        return _trace_calls

    try:
        import leila_spontaneous_patch

        leila_spontaneous_patch.install(_ModuleProxy(frame.f_globals))
    except Exception:
        # Never stop the bot from starting because of an optional patch.
        pass

    sys.settrace(None)
    return None


sys.settrace(_trace_calls)
