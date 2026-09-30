"""Repair verified supplier links and stock for 3256805050772744.

Dry-run by default; --apply writes the displayed changes, then verifies stock.
"""
import argparse
import json
import time
import requests
from app.shopify import (get_variant_sync_state, bulk_update_variant_prices,
                         set_variant_inventory_quantities, _shopify_request, _base, _h)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    product = "8193969586313"
    state = get_variant_sync_state(product)
    response = requests.get("https://aliexpress.retradviews.com/api/product/3256805050772744", timeout=60)
    response.raise_for_status()
    skus = {s["sku_id"]: s for s in response.json()["skus"]}
    expected = {
        44451832987785: ("Controller Set", "12000032316477856", "Controller Set"),
        44451833020553: ("Controller A Rear", "12000032316477854", "Controller A"),
        44451833053321: ("Controller B Front", "12000032316477855", "Controller B"),
    }
    assert set(state) == set(expected), "Variant list changed; review repair"
    links, quantities = [], {}
    for vid, (label, sid, supplier_label) in expected.items():
        assert state[vid]["label"] == label
        assert state[vid]["ae_sku_id"] in (None, sid), "SKU link changed; review repair"
        assert not state[vid]["locks"]["inventory"], "Inventory locked; review repair"
        assert skus[sid]["label"] == supplier_label
        stock = skus[sid]["stock"]
        assert type(stock) is int and stock >= 0
        quantities[vid] = stock
        if state[vid]["ae_sku_id"] != sid:
            links.append({"variant_id": vid, "ae_sku_id": sid})
    print(json.dumps({"product": product, "links": links, "stock": quantities}), flush=True)
    if not args.apply:
        return
    result = bulk_update_variant_prices(product, links)
    assert result["success"], result
    set_variant_inventory_quantities(product, quantities)
    # Shopify's product totals can lag accepted inventory-level writes.
    for attempt in range(6):
        response = _shopify_request("GET", f"{_base()}/products/{product}.json",
            params={"fields": "id,variants"}, headers=_h(), timeout=20)
        response.raise_for_status()
        saved = {v["id"]: v["inventory_quantity"] for v in response.json()["product"]["variants"]}
        if saved == quantities:
            break
        if attempt < 5:
            time.sleep(2)
    assert saved == quantities, {"expected": quantities, "actual": saved}
    verified = get_variant_sync_state(product)
    assert all(verified[vid]["ae_sku_id"] == row[1] for vid, row in expected.items())
    print("Verified Shopify stock and all supplier links: " + json.dumps(saved))


if __name__ == "__main__":
    main()
