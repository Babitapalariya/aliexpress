"""Find exact supplier-metafield replacement candidates; never update records."""
import json
from pathlib import Path
from app.shopify import _graphql

root = Path(__file__).parent / "logs"
rows = json.loads((root / "dead_listing_verified_20261128.json").read_text(encoding="utf-8"))
missing = [r for r in rows if not r["shopify_exists"]]
supplier_ids = {str(i) for r in missing for i in (r["aliexpress_id"], r.get("replacement_aliexpress_id")) if i}
matches = {}
cursor = None
seen = set()
count = 0
while True:
    data = _graphql('''query SupplierOwnership($after: String) {
      products(first: 100, after: $after) {
        nodes { id title supplier: metafield(namespace: "aliexpress", key: "product_id") { value } }
        pageInfo { hasNextPage endCursor }
      }
    }''', {"after": cursor})
    connection = data["products"]
    for node in connection["nodes"]:
        count += 1
        supplier = (node.get("supplier") or {}).get("value")
        if supplier in supplier_ids:
            matches.setdefault(supplier, []).append(node)
    page = connection["pageInfo"]
    if not page["hasNextPage"]:
        break
    cursor = page["endCursor"]
    if not cursor or cursor in seen:
        raise RuntimeError("Shopify product pagination did not advance")
    seen.add(cursor)
    print(f"Checked {count} Shopify products", flush=True)
report = [{"source": r["source"], "record_id": r["id"], "aliexpress_id": r["aliexpress_id"],
           "stored_shopify_id": r["shopify_product_id"],
           "current_id_matches": matches.get(r["aliexpress_id"], []),
           "old_id_matches": matches.get(r.get("replacement_aliexpress_id"), [])} for r in missing]
(root / "dead_listing_candidates_20261128.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"products_scanned": count, "missing_records": len(missing),
                  "with_candidates": [r for r in report if r["current_id_matches"] or r["old_id_matches"]]}), flush=True)
