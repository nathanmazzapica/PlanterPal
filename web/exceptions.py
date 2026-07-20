class ErrNetwork(Exception):
    """Base class for network errors"""


class ErrHostUnreachable(ErrNetwork):
    pass


class ErrTimedOut(ErrNetwork):
    pass


class ErrConnectionReset(ErrNetwork):
    pass


class ErrHttpStatus(ErrNetwork):
    """The backend responded, but rejected the request."""

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(
            "backend rejected request with HTTP {}".format(status_code)
        )
