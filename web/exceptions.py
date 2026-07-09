class ErrNetwork(Exception):
    """Base class for network errors"""

class ErrHostUnreachable(ErrNetwork):
    pass

class ErrTimedOut(ErrNetwork):
    pass

class ErrConnectionReset(ErrNetwork):
    pass
