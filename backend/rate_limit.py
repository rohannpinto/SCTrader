"""Shared `slowapi` `Limiter` instance.

Lives in its own module rather than inside `backend/main.py` (where
CLAUDE.md's project structure otherwise places "rate limiter", alongside
the rest of app assembly) purely to avoid an import cycle: every router
module needs to reference the *same* `Limiter` object at *import time* --
`@limiter.limit(...)` is a decorator, applied the moment the router module
is first imported -- but `main.py` needs to import the router modules to
mount them. If the limiter lived in `main.py`, routers importing it would
require `main.py` to already be fully loaded, which can't be true while
`main.py` is itself still in the middle of importing those same routers.

`backend/main.py` is still where the limiter is actually wired into the
app: `app.state.limiter = limiter`, the `RateLimitExceeded` exception
handler, and `SlowAPIMiddleware` all live there.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
