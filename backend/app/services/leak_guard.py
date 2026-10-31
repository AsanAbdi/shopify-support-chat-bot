from app.agent.prompts import canary


class LeakGuard:
    """Streaming output filter: the hidden marker (canary) of the system prompt must never reach the customer.

    The marker can arrive split over several chunks, so the last `len(marker) - 1` characters are held back until
    more text proves they are not the beginning of it. Once the marker is seen, everything after it is dropped.
    """

    def __init__(self) -> None:
        self.secret = canary().lower()
        self.hold = len(self.secret) - 1
        self.pending = ""  # received but not yet released to the client
        self.blocked = False

    def feed(self, piece: str) -> str:
        """Takes the next chunk, returns the part that is safe to send now."""
        if self.blocked:
            return ""
        self.pending += piece
        if self.secret in self.pending.lower():
            self.blocked, self.pending = True, ""
            return ""
        if len(self.pending) <= self.hold:
            return ""
        safe, self.pending = self.pending[: -self.hold], self.pending[-self.hold :]
        return safe

    def flush(self) -> str:
        """End of the answer: whatever is still held back is safe (a marker would have been caught by `feed`)."""
        rest, self.pending = ("" if self.blocked else self.pending), ""
        return rest

    def reset(self) -> None:
        """Forget the held-back text: it belonged to a draft that is being discarded."""
        self.pending = ""
