"""Delete local records only after fresh supplier and Shopify checks."""
from fastapi import HTTPException
from .models import ImportedProduct, ProductMapping, PendingImport, AliExpressIdHistory
from .shopify_links import product_id
from . import shopify


def supplier_confirms_dead(raw, aliexpress_id, is_dead):
    if not isinstance(raw, dict) or raw.get("error_response"):
        return False
    # DS product.get returns this explicit business response without a result,
    # product ID or SKU list when all supplier SKUs are unsaleable. This is
    # different from a timeout, auth failure, empty response or rate limit.
    response = raw.get("aliexpress_ds_product_get_response")
    if isinstance(response, dict) and not response.get("result"):
        return (str(response.get("rsp_code")) == "604" and
                str(response.get("rsp_msg", "")).strip().casefold() == "all sku unsaleable")
    if str(raw.get("product_id")) != str(aliexpress_id) or not isinstance(raw.get("skus"), list):
        return False
    return is_dead(raw)


def delete_dead_listings(db, items, fetch_supplier, is_dead):
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise HTTPException(400, "Select between 1 and 10 listings per request")
    keys = []
    for item in items:
        if not isinstance(item, dict) or item.get("source") not in ("imported", "mapping"):
            raise HTTPException(400, "Invalid listing source")
        ident = item.get("id")
        if type(ident) is not int or ident <= 0:
            raise HTTPException(400, "Invalid listing ID")
        if (item["source"], ident) not in keys:
            keys.append((item["source"], ident))
    deleted, skipped = [], []
    for source, ident in keys:
        model = ImportedProduct if source == "imported" else ProductMapping
        try:
            row = db.query(model).filter(model.id == ident).first()
            if not row or not row.is_dead_listing:
                raise ValueError("Listing is absent or is not currently flagged dead")
            snapshot = (row.aliexpress_id, row.shopify_product_id)
            sid = product_id(row.shopify_product_id)
            if not sid:
                raise ValueError("No valid Shopify ID to verify; review this record manually")
            raw = fetch_supplier(row.aliexpress_id, db)
            if not supplier_confirms_dead(raw, row.aliexpress_id, is_dead):
                raise ValueError("AliExpress did not confirm a dead listing; it may be active or the response could not be verified")
            data = shopify._graphql(
                "query CleanupProduct($id: ID!) { product(id: $id) { id } }",
                {"id": f"gid://shopify/Product/{sid}"})
            if not isinstance(data, dict) or "product" not in data or data.get("errors"):
                raise ValueError("Shopify absence could not be verified")
            if data["product"] is not None:
                raise ValueError("Product still exists in Shopify")

            # Supplier requests can take time. Re-read and lock the local row
            # so a concurrent remap cannot be deleted using stale verification.
            db.rollback()
            row = db.query(model).filter(model.id == ident).with_for_update().first()
            if not row or not row.is_dead_listing or (row.aliexpress_id, row.shopify_product_id) != snapshot:
                raise ValueError("Listing changed during verification; refresh and retry")
            db.query(AliExpressIdHistory).filter(
                AliExpressIdHistory.source == source, AliExpressIdHistory.record_id == ident
            ).delete(synchronize_session=False)
            db.delete(row)
            db.flush()
            # Remove its retry queue only when no remaining record owns that
            # supplier ID. Keep another product's mapping/queue untouched.
            owners = any(db.query(owner).filter(owner.aliexpress_id == snapshot[0]).first()
                         for owner in (ImportedProduct, ProductMapping))
            if not owners:
                db.query(PendingImport).filter(PendingImport.aliexpress_id == snapshot[0]).delete(synchronize_session=False)
            db.commit()
            deleted.append({"source": source, "id": ident})
        except Exception as exc:
            db.rollback()
            reason = str(exc) if isinstance(exc, ValueError) else "Verification or deletion failed; record was retained"
            skipped.append({"source": source, "id": ident, "reason": reason})
    return {"deleted": deleted, "skipped": skipped,
            "message": f"Deleted {len(deleted)} local listings; skipped {len(skipped)}"}
