# restore_variants.py
import requests
from app.database import SessionLocal
from app.models import ProductMapping
from app.aliexpress import get_product
from app.shopify import get_shopify_token, _base

def restore_variants_for_mapping(mapping):
    # Fetch AliExpress product
    raw = get_product(mapping.aliexpress_id, None)  # db not needed, token handled inside
    aliexpress_skus = raw.get("skus", [])
    if not aliexpress_skus:
        print(f"No SKUs for {mapping.aliexpress_id}")
        return

    # Fetch current Shopify product (may have missing variants)
    token = get_shopify_token()
    url = f"{_base()}/products/{mapping.shopify_product_id}.json"
    res = requests.get(url, headers={"X-Shopify-Access-Token": token})
    res.raise_for_status()
    shopify_product = res.json()["product"]
    existing_variants = {v.get("option1", ""): v for v in shopify_product.get("variants", [])}

    # Build new variants list: keep existing, add missing from AliExpress
    new_variants = []
    for sku in aliexpress_skus:
        sku_attr = sku.get("sku_attr", "")
        # extract option name
        import re
        match = re.search(r'#([^;]+)', sku_attr)
        option_name = match.group(1) if match else sku_attr
        price = sku.get("sale_price") or sku.get("price")
        if option_name in existing_variants:
            # update price of existing variant
            var = existing_variants[option_name]
            var["price"] = str(price)
            new_variants.append(var)
        else:
            # create new variant (only price, option, no inventory)
            new_variants.append({
                "option1": option_name,
                "price": str(price),
                "inventory_management": None,
                "inventory_quantity": 0,
            })
    # Add any existing variants that were not matched (should not happen, but safe)
    for opt, var in existing_variants.items():
        if not any(v.get("option1") == opt for v in new_variants):
            new_variants.append(var)

    # Update product with full variant list
    payload = {"product": {"variants": new_variants}}
    put_res = requests.put(url, json=payload, headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"})
    put_res.raise_for_status()
    print(f"Restored/updated {len(new_variants)} variants for product {mapping.shopify_product_id}")

if __name__ == "__main__":
    db = SessionLocal()
    mappings = db.query(ProductMapping).all()
    for m in mappings:
        print(f"Processing {m.aliexpress_id} -> {m.shopify_product_id}")
        restore_variants_for_mapping(m)
    db.close()