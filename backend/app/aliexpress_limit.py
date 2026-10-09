"""Serialize calls and honor an AliExpress app-level cooldown per worker."""
import math
import re
import threading
import time
from fastapi import HTTPException

_lock = threading.Lock()
_blocked_until = 0.0
_next_call = 0.0


def _cooldown_error(seconds):
    # Round up so the displayed wait never suggests retrying before expiry.
    minutes = max(1, math.ceil(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    wait = " ".join(parts)
    return HTTPException(429, detail={
        "aliexpress_error_code": "AppApiCallLimit",
        "aliexpress_message": f"AliExpress API access is temporarily limited. Try again in {wait}. This affects the app, not this product.",
        "retry_after_seconds": seconds,
    }, headers={"Retry-After": str(seconds)})


def guarded_call(callback):
    global _blocked_until, _next_call
    with _lock:
        remaining = math.ceil(_blocked_until - time.monotonic())
        if remaining > 0:
            raise _cooldown_error(remaining)
        wait = _next_call - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            result = callback()
        finally:
            _next_call = time.monotonic() + 1.0
        error = result.get("error_response", {}) if isinstance(result, dict) else {}
        if "AppApiCallLimit" in (error.get("code"), error.get("sub_code")):
            message = " ".join(str(error.get(key) or "") for key in ("msg", "message", "sub_msg", "sub_message"))
            match = re.search(r"(\d+)\s*seconds?", message, re.I)
            seconds = max(1, int(match.group(1))) if match else 60
            _blocked_until = time.monotonic() + seconds
            raise _cooldown_error(seconds)
        return result
