"""Błędy komunikacji bez ujawniania zawartości ramek."""
class NotConnectedError(ConnectionError):
    pass

class ConnectionClosedError(ConnectionError):
    """Druga strona zamknęła połączenie TCP."""

class MissingCredentialsError(ValueError):
    def __init__(self, fields):
        self.fields = fields
        super().__init__(", ".join(fields))
