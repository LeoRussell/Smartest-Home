"""Errors raised by the constrained automation interpreter."""

class AutomationExecutionError(Exception):
    pass


class AutomationStoppedError(AutomationExecutionError):
    pass
