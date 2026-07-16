from app.state import State
from web.client import Client
from collections import deque
from config import OUTBOX_LEN


class Outbox():
    def __init__(self, client: Client):
        self.client = client # TODO: change web.client to a class
        # MicroPython's deque signature is deque(iterable, maxlen[, flags]) —
        # both args required and positional, and the iterable must be empty.
        self.queue: deque[State] = deque((), OUTBOX_LEN)


    def post(self, state: State):
        # TODO: handle queue full
        self.queue.append(state)

    def drain(self):
        # Drain oldest-first, removing each item as it is delivered so a
        # later drain doesn't re-send readings already reported.
        while self.queue:
            state = self.queue.popleft()
            try:
                self.client.report(state)
            except:
                # Delivery failed: put it back at the front and stop draining.
                self.queue.appendleft(state)
                raise

