"""Product-independent supplier link inspection and explicit repair."""
from fastapi import HTTPException
from . import shopify


def inspect_links(product_id, skus):
    state = shopify.get_variant_sync_state(product_id)
    matches = shopify._match_supplier_variants(state, skus)
    return {
        "variants": [{"variant_id": str(vid), "label": row["label"],
                      "current_sku_id": row["ae_sku_id"],
                      "suggested_sku_id": str(matches[vid]["sku_id"]) if vid in matches else None}
                     for vid, row in state.items()],
        "supplier_options": [{"sku_id": str(s["sku_id"]), "label": s.get("label") or str(s["sku_id"])}
                             for s in skus if s.get("sku_id") is not None],
    }


def save_links(product_id, skus, links):
    state = shopify.get_variant_sync_state(product_id)
    supplier_ids = [str(s["sku_id"]) for s in skus if s.get("sku_id") is not None]
    if not isinstance(links, list) or len(links) != len(state) or not state:
        raise HTTPException(400, "Select a supplier option for every Shopify variant")
    updates, seen_variants, seen_skus = [], set(), set()
    for link in links:
        try:
            vid, sid = int(link["variant_id"]), str(link["sku_id"])
        except (TypeError, ValueError, KeyError):
            raise HTTPException(400, "Invalid supplier link")
        if vid not in state or vid in seen_variants or sid in seen_skus or supplier_ids.count(sid) != 1:
            raise HTTPException(400, "Each variant needs a different, valid supplier SKU from this product")
        # Do not overwrite another user's link repair from an outdated dialog.
        if link.get("current_sku_id") != state[vid]["ae_sku_id"]:
            raise HTTPException(409, "Supplier links changed; reopen Repair supplier links and try again")
        update = {"variant_id": vid, "ae_sku_id": sid, "sku_link_verified": True}
        if state[vid]["ae_sku_id"] != sid:
            update.update(supplier_history=None, supplier_history_id=state[vid].get("supplier_history_id"))
        updates.append(update)
        seen_variants.add(vid)
        seen_skus.add(sid)
    for offset in range(0, len(updates), 100):
        result = shopify.bulk_update_variant_prices(product_id, updates[offset:offset + 100])
        if not result["success"]:
            raise HTTPException(502, f"Could not save supplier links: {result['errors']}")
    return {"message": f"Saved {len(updates)} supplier links. You can now retry sync.", "updated": len(updates)}
