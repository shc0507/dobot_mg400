"""Errors shared by services and their HTTP clients."""


class ServiceError(Exception):
    """Carry an HTTP status and a message without retrying the operation."""

    def __init__(self, status, message):
        self.status = status
        self.message = str(message)
        super().__init__(self.message)
