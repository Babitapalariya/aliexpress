"""Keep supplier IDs searchable across repeated remaps in either table."""
from sqlalchemy import exists
from .models import AliExpressIdHistory


def history_matches(model, source, term):
    return exists().where(
        AliExpressIdHistory.source == source,
        AliExpressIdHistory.record_id == model.id,
        AliExpressIdHistory.aliexpress_id.ilike(term),
    )


def remember_supplier_ids(db, source, record):
    for supplier_id in {record.aliexpress_id, getattr(record, "replacement_aliexpress_id", None)}:
        if not supplier_id:
            continue
        existing = db.query(AliExpressIdHistory).filter_by(
            source=source, record_id=record.id, aliexpress_id=supplier_id,
        ).first()
        if existing is None:
            db.add(AliExpressIdHistory(source=source, record_id=record.id, aliexpress_id=supplier_id))
    # The caller commits this history together with the new current ID.
