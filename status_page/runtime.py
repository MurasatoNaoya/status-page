"""Shared runtime context for blueprints.

Routes consume this instead of importing `status_page.app` directly.
"""


def _default_runtime_context_provider():
    return {}


_runtime_context_provider = _default_runtime_context_provider


def set_runtime_context_provider(provider):
    global _runtime_context_provider
    _runtime_context_provider = provider


def get_runtime_context():
    return _runtime_context_provider()
