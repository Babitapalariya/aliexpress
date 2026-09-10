"""Supplier movements are recorded independently from manual adjustments."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


def observe_supplier_price(previous, price):
    current = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not current.is_finite() or current < 0:
        raise ValueError("Invalid supplier price")
    if previous and Decimal(previous["price"]) == current:
        return previous
    old = previous.get("price") if previous else None
    return {
        "price": str(current),
        "change": str(current - Decimal(old)) if old is not None else None,
        "changed_at": datetime.now(timezone.utc).isoformat() if old is not None else None,
    }
