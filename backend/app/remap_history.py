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


def validate_remap_target(db, source, record, supplier_id):
    """Reject IDs owned by another record, including previous remap IDs."""
    from fastapi import HTTPException
    from .models import ImportedProduct, ProductMapping
    for kind, model in (("imported", ImportedProduct), ("mapping", ProductMapping)):
        historical = exists().where(
            AliExpressIdHistory.source == kind,
            AliExpressIdHistory.record_id == model.id,
            AliExpressIdHistory.aliexpress_id == supplier_id,
        )
        condition = (model.aliexpress_id == supplier_id) | historical
        if kind == "imported":
            condition = condition | (model.replacement_aliexpress_id == supplier_id)
        query = db.query(model).filter(condition)
        if kind == source:
            query = query.filter(model.id != record.id)
        conflict = query.first()
        if conflict is not None:
            owner = f"Shopify product {conflict.shopify_product_id}" if conflict.shopify_product_id else f"{kind} record {conflict.id}"
            raise HTTPException(409, f"AliExpress ID {supplier_id} is already used or was previously remapped by {owner}. Choose an unused AliExpress ID.")
