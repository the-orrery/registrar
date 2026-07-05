"""Domain errors."""


class RegistrarError(Exception):
    """Error with a user-facing message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
