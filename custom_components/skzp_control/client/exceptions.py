"""Błędy komunikacji bez ujawniania zawartości ramek."""
class NotConnectedError(ConnectionError):
    pass

class MissingCredentialsError(ValueError):
    def __init__(self, fields):
        self.fields = fields
        super().__init__(", ".join(fields))
