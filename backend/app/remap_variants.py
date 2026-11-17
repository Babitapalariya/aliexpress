"""Replace supplier variants without overwriting merchant product content."""
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from . import shopify


def _sync_replacement_inventory(product_id, stocks, location_id):
    """Read all newly created inventory IDs, then connect and set their stock."""
    query = """query RemapInventory($id: ID!, $after: String) {
      product(id: $id) { variants(first: 100, after: $after) {
        nodes { inventoryItem { id }
          skuId: metafield(namespace: "aliexpress", key: "sku_id") { value } }
        pageInfo { hasNextPage endCursor }
      } }
    }"""
    items, after, cursors = {}, None, set()
    while True:
        data = shopify._graphql(query, {"id": f"gid://shopify/Product/{product_id}", "after": after})
        connection = (data.get("product") or {}).get("variants")
        if not connection:
            raise ValueError("Could not verify replacement variants")
        for node in connection.get("nodes", []):
            sid = (node.get("skuId") or {}).get("value")
            iid = (node.get("inventoryItem") or {}).get("id")
            if sid not in stocks or sid in items or not iid:
                raise ValueError("Replacement variant SKU links are incomplete or duplicated")
            items[sid] = int(iid.rsplit("/", 1)[-1])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after or after in cursors:
            raise ValueError("Could not read all replacement variant pages")
        cursors.add(after)
    if set(items) != set(stocks):
        raise ValueError(f"Shopify returned {len(items)} of {len(stocks)} replacement variants")
    for sid, iid in items.items():
        # This endpoint connects an unstocked item before setting its quantity.
        response = shopify._shopify_request("POST", f"{shopify._base()}/inventory_levels/set.json",
            json={"location_id": location_id, "inventory_item_id": iid, "available": stocks[sid]},
            headers=shopify._h(), timeout=20)
        response.raise_for_status()
        level = response.json().get("inventory_level") or {}
        if level.get("available") != stocks[sid] or level.get("inventory_item_id") != iid or level.get("location_id") != location_id:
            raise ValueError(f"Shopify did not confirm stock for supplier SKU {sid}")


def replace_supplier_variants(product_id, supplier, supplier_id):
    skus = supplier.get("skus") or []
    if not skus:
        raise HTTPException(400, "Replacement listing has no variants")
    options, values = shopify.build_supplier_options(skus)
    variants, seen, stocks = [], set(), {}
    for sku, row in zip(skus, values):
        sid = str(sku.get("sku_id") or "")
        try:
            raw_price = sku.get("sale_price")
            price = Decimal(str(sku.get("price") if raw_price is None else raw_price))
            stock = Decimal(str(sku.get("stock")))
            if not sid or sid in seen or not price.is_finite() or price < 0:
                raise ValueError()
            if not stock.is_finite() or stock < 0 or stock != stock.to_integral_value() or stock > 2147483647:
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            raise HTTPException(400, "Replacement variants need unique SKU IDs, valid prices and whole-number stock")
        seen.add(sid)
        stocks[sid] = int(stock)
        variants.append({
            "price": str(price.quantize(Decimal("0.01"))),
            "optionValues": [{"optionName": option["name"], "name": value} for option, value in zip(options, row)],
            "inventoryItem": {"tracked": True},
            "inventoryPolicy": "DENY",
            "metafields": [{"namespace": "aliexpress", "key": "sku_id", "type": "single_line_text_field", "value": sid}],
        })
    locations = shopify._shopify_request("GET", f"{shopify._base()}/locations.json", headers=shopify._h(), timeout=20)
    locations.raise_for_status()
    active = [loc for loc in locations.json().get("locations", []) if loc.get("active", True) and not loc.get("legacy", False)]
    if not active:
        raise HTTPException(409, "No active Shopify stock location; remap stopped")
    gid = f"gid://shopify/Product/{product_id}"
    # Supplying only options and variants preserves title, description and other product content.
    payload = {"id": gid, "productOptions": [
        {"name": option["name"], "position": i, "values": [{"name": value} for value in option["values"]]}
        for i, option in enumerate(options, 1)], "variants": variants}
    query = """mutation RemapVariants($input: ProductSetInput!) {
      productSet(input: $input, synchronous: true) {
        product { id } userErrors { field message }
      }
    }"""
    # Never blindly retry a timeout/5xx: the replacement may already have completed.
    response = shopify._shopify_request("POST", shopify._graphql_url(),
        json={"query": query, "variables": {"input": payload}}, headers=shopify._h(),
        timeout=120, retry_ambiguous=False)
    response.raise_for_status()
    result = response.json()
    changed = (result.get("data") or {}).get("productSet") or {}
    errors = result.get("errors") or changed.get("userErrors")
    if errors or (changed.get("product") or {}).get("id") != gid:
        raise HTTPException(502, f"Shopify variant replacement failed: {errors or 'missing product confirmation'}")
    shopify._invalidate_lock_cache(str(product_id))
    # Product metafields use a separate mutation to preserve unrelated existing metafields.
    warnings = []
    inventory_updated = False
    try:
        _sync_replacement_inventory(product_id, stocks, active[0]["id"])
        inventory_updated = True
    except Exception as exc:
        warnings.append("Variants replaced, but stock sync is incomplete. Retry stock sync or remap the same AliExpress ID: " + str(getattr(exc, "detail", exc)))
    metafield_updated = False
    try:
        result = shopify._graphql("""mutation RemapSupplier($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) { userErrors { field message } }
        }""", {"metafields": [{"ownerId": gid, "namespace": "aliexpress", "key": "product_id",
                               "type": "single_line_text_field", "value": str(supplier_id)}]})
        if "metafieldsSet" not in result or result["metafieldsSet"].get("userErrors"):
            raise ValueError(str(result))
        metafield_updated = True
    except Exception as exc:
        warnings.append("Variants replaced, but supplier product link update failed: " + str(getattr(exc, "detail", exc)))
    images_updated = True
    image_result = {"attached": 0, "remaining": 0}
    resolved_skus = shopify.resolve_supplier_images(skus)
    if any(sku.get("image") for sku in resolved_skus):
        try:
            image_result = shopify.backfill_sku_images(str(product_id), resolved_skus)
            images_updated = image_result.get("remaining", 0) == 0
            if not images_updated:
                warnings.append("Variants replaced, but some SKU images are missing. Retry Sync images in the edit form.")
        except Exception as exc:
            images_updated = False
            warnings.append("Variants replaced, but SKU image sync failed: " + str(getattr(exc, "detail", exc)))
    return {"shopify_price_updated": True, "shopify_inv_updated": inventory_updated,
            "shopify_variants_replaced": True, "shopify_metafield_updated": metafield_updated,
            "shopify_images_updated": images_updated, "sku_images": image_result,
            "variant_count": len(variants), "warnings": warnings}
