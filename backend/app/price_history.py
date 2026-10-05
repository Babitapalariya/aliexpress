"""Supplier movements are recorded independently from manual adjustments."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


def observe_supplier_price(previous, price):
    current = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not current.is_finite() or current < 0:
        raise ValueError("Invalid supplier price")
    old = previous.get("price") if previous else None
    change = current - Decimal(old) if old is not None else None
    changed_at = datetime.now(timezone.utc).isoformat() if change else None
    prior = previous or {}
    last_change = prior.get("last_change", prior.get("change"))
    last_changed_at = prior.get("last_changed_at", prior.get("changed_at"))
    last_from = prior.get("last_from")
    last_to = prior.get("last_to")
    # Older records kept only the most recent comparison. Recover its price
    # pair when a non-zero change is still present; never invent lost history.
    if last_change is not None and Decimal(str(last_change)) != 0 and last_from is None and old is not None:
        last_to = str(Decimal(old).quantize(Decimal("0.01")))
        last_from = str((Decimal(old) - Decimal(str(last_change))).quantize(Decimal("0.01")))
    if change:
        last_change, last_changed_at = str(change), changed_at
        last_from, last_to = str(Decimal(old).quantize(Decimal("0.01"))), str(current)
    elif last_change is None and change is not None:
        last_change = "0.00"
    return {
        "price": str(current),
        # Keep the per-check comparison for sync diagnostics, independently of
        # the last actual movement displayed in the edit forms.
        "change": str(change) if change is not None else None,
        "changed_at": changed_at,
        "last_change": last_change,
        "last_changed_at": last_changed_at,
        "last_from": last_from,
        "last_to": last_to,
    }


def supplier_change_summary(history):
    history = history or {}
    return {
        "supplier_price_change": history.get("last_change", history.get("change")),
        "supplier_price_changed_at": history.get("last_changed_at", history.get("changed_at")),
        "supplier_previous_price": history.get("last_from"),
        "supplier_current_price": history.get("last_to"),
    }
