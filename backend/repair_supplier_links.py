"""Reusable supplier-link repair. Dry-run by default; preserves prices.

Usage: python repair_supplier_links.py ALIEXPRESS_ID --link VARIANT_ID=SKU_ID
       [--link VARIANT_ID=SKU_ID ...] [--push-stock] [--apply]
Explicit selections override suggestions; all variants must be matched uniquely.
"""
import argparse
import json
import requests
from app import shopify, sku_links


def get(path, **params):
    response = requests.get("https://aliexpress.retradviews.com/api/" + path, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aliexpress_id")
    parser.add_argument("--link", action="append", default=[])
    parser.add_argument("--push-stock", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    products = set()
    for path, key in [("mappings/list", "mappings"), ("dashboard/products", "products")]:
        for row in get(path, search=args.aliexpress_id, page_size=100).get(key, []):
            if str(row["aliexpress_id"]) == args.aliexpress_id and row.get("shopify_product_id"):
                products.add(str(row["shopify_product_id"]))
    if len(products) != 1:
        raise RuntimeError("Expected exactly one Shopify product for this AliExpress ID")
    product = products.pop()
    skus = get("product/" + args.aliexpress_id)["skus"]
    info = sku_links.inspect_links(product, skus)
    choices = {}
    for item in args.link:
        vid, sid = item.split("=", 1)
        if vid in choices:
            raise ValueError("Duplicate variant selection")
        choices[vid] = sid
    if set(choices) - {v["variant_id"] for v in info["variants"]}:
        raise ValueError("A selected variant is not in this product")
    supplier = {str(s["sku_id"]): s for s in skus}
    links = [{"variant_id": v["variant_id"], "sku_id": choices.get(v["variant_id"], v["suggested_sku_id"]),
              "current_sku_id": v["current_sku_id"]} for v in info["variants"]]
    if any(link["sku_id"] not in supplier for link in links) or len({l["sku_id"] for l in links}) != len(links):
        print(json.dumps(info, indent=2))
        raise ValueError("Choose a different supplier SKU for every variant with --link")
    print(json.dumps({"product": product, "plan": [
        {"variant": v["label"], "supplier": supplier[l["sku_id"]]["label"],
         "stock": supplier[l["sku_id"]].get("stock"), **l}
        for v, l in zip(info["variants"], links)]}, indent=2), flush=True)
    if not args.apply:
        return
    result = sku_links.save_links(product, skus, links)
    print(result["message"], flush=True)
    verified = shopify.get_variant_sync_state(product)
    if any(verified[int(l["variant_id"])]["ae_sku_id"] != l["sku_id"] or
           not verified[int(l["variant_id"])]["sku_link_verified"] for l in links):
        raise RuntimeError("Shopify supplier-link verification failed")
    if args.push_stock:
        shopify.update_shopify_product_inventory_with_skus(product, skus, strict=True)
        print("Shopify confirmed supplier stock writes for unlocked variants.")
    print("Supplier links verified. Prices and adjustments were not edited.")


if __name__ == "__main__":
    main()
