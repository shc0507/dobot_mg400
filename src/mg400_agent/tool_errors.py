"""Errors from tool validation before any device action."""


class ToolRejectedError(ValueError):
    """The tool arguments or target were rejected before execution."""
