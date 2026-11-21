"""Cancellation shared by the microscope motion and capture algorithms."""


class SideCaptureCancelled(RuntimeError):
    """The operator cancelled a microscope motion or camera acquisition."""
