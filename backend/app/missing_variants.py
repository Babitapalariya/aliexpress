"""Restore missing supplier variants without replacing existing Shopify IDs."""
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from . import shopify


def plan_missing_variants(product, state, price_increase=0):
    skus = product.get("skus") or []
    ids = [str(s.get("sku_id") or "") for s in skus]
    if not ids or "" in ids or len(set(ids)) != len(ids):
        raise HTTPException(409, "Supplier SKU IDs are missing or duplicated")
    if not state or len(skus) > 100:
        raise HTTPException(409, "This repair requires an existing product with up to 100 supplier variants")
    linked = [str(v.get("ae_sku_id") or "") for v in state.values()]
    if len(set(linked)) != len(linked) or any(sid not in ids for sid in linked):
        raise HTTPException(409, "Repair supplier links first: every existing variant must link to a different current supplier SKU")
    options, rows = shopify.build_supplier_options(skus)
    by_sid = {str(v["ae_sku_id"]): vid for vid, v in state.items()}
    variants, missing = [], []
    try:
        increase = Decimal(str(price_increase or 0))
        if not increase.is_finite():
            raise ValueError()
        for sku, row in zip(skus, rows):
            sid = str(sku["sku_id"])
            values = {f"option{i}": row[i-1] if i <= len(row) else None for i in range(1, 4)}
            if sid in by_sid:
                # Never send prices, stock, images or metafields for existing IDs.
                variants.append({"id": by_sid[sid], **values})
                continue
            price = Decimal(str(sku.get("sale_price") or sku.get("price"))) + increase
            stock = Decimal(str(sku.get("stock")))
            if not price.is_finite() or price < 0 or not stock.is_finite() or stock < 0 or stock != stock.to_integral_value():
                raise ValueError()
            fields = [{"namespace": "aliexpress", "key": "sku_id", "type": "single_line_text_field", "value": sid}]
            if increase:
                fields.append({"namespace": "sync", "key": "price_increase", "type": "number_decimal", "value": str(increase)})
            variants.append({**values, "price": str(price.quantize(Decimal("0.01"))),
                             "inventory_management": "shopify", "inventory_policy": "deny",
                             "metafields": fields})
            missing.append({"sku_id": sid, "label": sku.get("label"), "price": str(price), "stock": int(stock)})
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(409, "Missing variants need valid supplier prices and whole-number stock; refresh and retry")
    return {"options": options, "variants": variants, "missing": missing}


def restore_missing_variants(product_id, product, price_increase=0, *, dry_run=False):
    before = shopify.get_variant_sync_state(product_id)
    plan = plan_missing_variants(product, before, price_increase)
    if dry_run:
        return {"product_id": str(product_id), "existing": len(before), "missing": plan["missing"]}
    if not plan["missing"]:
        return {"message": "All supplier variants already exist in Shopify", "added": 0, "total": len(before), "warnings": []}

    # Verify the full current ID list before sending a complete REST variant list.
    response = shopify._shopify_request("GET", f"{shopify._base()}/products/{product_id}.json",
        params={"fields": "id,variants"}, headers=shopify._h(), timeout=30)
    response.raise_for_status()
    current = response.json().get("product", {}).get("variants", [])
    if {v["id"] for v in current} != set(before):
        raise HTTPException(409, "Shopify variants changed; refresh before repairing")
    response = shopify._shopify_request("PUT", f"{shopify._base()}/products/{product_id}.json",
        json={"product": {"id": product_id, "options": plan["options"], "variants": plan["variants"]}},
        headers=shopify._h(), timeout=60, retry_ambiguous=False)
    response.raise_for_status()
    after = shopify.get_variant_sync_state(product_id)
    expected = {str(s["sku_id"]) for s in product["skus"]}
    if len(after) != len(expected) or {v["ae_sku_id"] for v in after.values()} != expected or not set(before).issubset(after):
        raise HTTPException(502, "Variant creation could not be verified; reload before retrying")
    for vid, old in before.items():
        if any(after[vid].get(key) != old.get(key) for key in ("price", "price_increase", "locks", "ae_sku_id")):
            raise HTTPException(502, "An existing variant changed during repair; review the product")
    warnings = []
    stocks = {m["sku_id"]: m["stock"] for m in plan["missing"]}
    try:
        shopify.set_variant_inventory_quantities(product_id, {
            vid: stocks[v["ae_sku_id"]] for vid, v in after.items() if v["ae_sku_id"] in stocks})
    except Exception as exc:
        warnings.append(f"Variants created; retry stock sync: {exc}")
    try:
        images = shopify.backfill_sku_images(product_id, product["skus"])
        if images.get("remaining"):
            warnings.append("Some variant images remain missing; retry image sync")
    except Exception as exc:
        warnings.append(f"Variants created; retry image sync: {exc}")
    return {"message": f"Added {len(stocks)} missing variants; {len(after)} variants now in Shopify",
            "added": len(stocks), "total": len(after), "warnings": warnings}
