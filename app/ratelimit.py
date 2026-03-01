import time
from collections import defaultdict, deque

class RateLimiter:
    def __init__(self, per_minute: int = 1):
        self.per_minute = per_minute
        self.hits = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self.hits[key]
        while q and (now - q[0] > 60):
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True
