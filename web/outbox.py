from app.state import State
from web.client import Client
from collections import deque
from config import OUTBOX_LEN


class Outbox():
    def __init__(self, client: Client):
        self.client = client # TODO: change web.client to a class
        self.queue: deque[State] = deque(maxlen=OUTBOX_LEN)


    def post(self, state: State):
        # TODO: handle queue full
        self.queue.append(state)

    def drain(self):
        for state in self.queue:
            try:
                self.client.report(state)
            except:
                # TODO: handle errors
                raise

