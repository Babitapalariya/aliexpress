"""Targeted repair for the reported tire listing; dry-run unless --apply is used.

Run from backend with the configured Shopify credentials. Saves a before snapshot
under logs before writing. Does not invoke the dashboard's markup-resetting sync.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import requests

from app.shopify import (
    _match_supplier_variants, bulk_update_variant_prices, get_variant_sync_state,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    product_id = "8830234165385"
    state = get_variant_sync_state(product_id)
    response = requests.get("https://aliexpress.retradviews.com/api/product/3256809169795471", timeout=60)
    response.raise_for_status()
    skus = response.json()["skus"]
    vid, sku_id = 48284037054601, "12000048851963710"
    variant = state[vid]
    assert variant["label"] == "Inner Outer Tires Set", "Variant title changed; review repair"
    assert variant["ae_sku_id"] in (None, sku_id), "Existing SKU link differs; review repair"
    assert any(s["sku_id"] == sku_id and s["label"].casefold() == "inner outer tires" for s in skus)
    assert variant["price_increase"] in (Decimal("10.74"), Decimal("10")), "Adjustment changed; review repair"
    update = {"variant_id": vid, "ae_sku_id": sku_id, "price_increase": "10.00",
              "increase_metafield_id": variant["increase_metafield_id"]}
    if not variant["locks"]["price"]:
        update["price"] = str((Decimal(variant["price"]) - variant["price_increase"] + Decimal("10")).quantize(Decimal("0.01")))
    proposed = {key: dict(value) for key, value in state.items()}
    proposed[vid]["ae_sku_id"] = sku_id
    assert len(_match_supplier_variants(proposed, skus)) == len(state), "Unresolved SKU links"
    print(json.dumps({"product": product_id, "changes": [update], "all_supplier_links_verified": True}))
    if not args.apply:
        return
    directory = Path(__file__).parent / "logs"
    directory.mkdir(exist_ok=True)
    backup = directory / ("tire-repair-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    backup.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")
    result = bulk_update_variant_prices(product_id, [update])
    if not result["success"]:
        raise RuntimeError(result)
    saved = get_variant_sync_state(product_id)
    assert saved[vid]["ae_sku_id"] == sku_id
    assert saved[vid]["price_increase"] == Decimal("10")
    if "price" in update:
        assert Decimal(saved[vid]["price"]) == Decimal(update["price"])
    assert len(_match_supplier_variants(saved, skus)) == len(saved)
    print("Verified saved $10 adjustment and all three supplier SKU links.")


if __name__ == "__main__":
    main()
