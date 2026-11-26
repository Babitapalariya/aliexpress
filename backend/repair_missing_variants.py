"""Repair the four reported products; preview by default, --apply to restore."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import requests

from app.aliexpress import get_product
from app.shopify import get_variant_sync_state
from app.missing_variants import restore_missing_variants

PAIRS = [("9012333740169", "3256812460317394"), ("9012700512393", "3256809391427903"),
         ("9012913045641", "3256811495422423"), ("9012177830025", "3256809598530773")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for pid, aid in PAIRS:
        response = requests.get("https://aliexpress.retradviews.com/api/dashboard/products/lookup",
                                params={"q": aid, "source": "imported"}, timeout=60)
        response.raise_for_status()
        matches = [r for r in response.json().get("results", [])
                   if str(r.get("shopify_product_id")) == pid and str(r.get("aliexpress_id")) == aid]
        if len(matches) != 1:
            raise RuntimeError(f"Could not verify live imported product {pid}")
        product = get_product(aid)
        increase = matches[0].get("price_increase") or 0
        plan = restore_missing_variants(pid, product, increase, dry_run=True)
        print(json.dumps(plan), flush=True)
        if not args.apply:
            continue
        backup = Path(__file__).parent / "logs" / f"variant_repair_{pid}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
        backup.parent.mkdir(exist_ok=True)
        backup.write_text(json.dumps({"record": matches[0], "supplier": product,
            "before": get_variant_sync_state(pid), "plan": plan}, default=str, indent=2), encoding="utf-8")
        result = restore_missing_variants(pid, product, increase)
        print(json.dumps(result), flush=True)
        state = get_variant_sync_state(pid)
        print(json.dumps({"verified_product": pid, "variants": len(state), "sku_ids": [r["ae_sku_id"] for r in state.values()]}), flush=True)
        if result.get("warnings"):
            raise RuntimeError("Review repair warnings before proceeding to the next product")


if __name__ == "__main__":
    main()
