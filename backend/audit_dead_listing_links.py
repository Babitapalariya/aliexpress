"""Read-only live API/Shopify ownership audit; no mapping changes."""
import json
from pathlib import Path
from collections import Counter
import requests
from app.shopify import _graphql


def main():
    rows = []
    for source, key in (("dead-listings", "products"), ("dead-mappings", "mappings")):
        response = requests.get(f"https://aliexpress.retradviews.com/api/{source}", timeout=60)
        response.raise_for_status()
        rows.extend(dict(row, source=source) for row in response.json()[key])
    ids = list(dict.fromkeys(str(r["shopify_product_id"]) for r in rows if r.get("shopify_product_id")))
    nodes = []
    query = '''query CheckDeadProducts($ids: [ID!]!) {
      nodes(ids: $ids) { ... on Product { id title handle
        supplier: metafield(namespace: "aliexpress", key: "product_id") { value }
      } }
    }'''
    for offset in range(0, len(ids), 50):
        data = _graphql(query, {"ids": ["gid://shopify/Product/" + i for i in ids[offset:offset+50]]})
        nodes.extend(data["nodes"])
    by_id = {n["id"].rsplit("/", 1)[-1]: n for n in nodes if n}
    for row in rows:
        node = by_id.get(str(row.get("shopify_product_id")))
        supplier = (node.get("supplier") or {}).get("value") if node else None
        row.update(shopify_exists=bool(node), shopify_title=node.get("title") if node else None,
                   shopify_supplier_id=supplier, supplier_matches=supplier == row["aliexpress_id"])
    path = Path(__file__).parent / "logs" / "dead_listing_verified_20261128.json"
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps({"counts": dict(Counter(r["source"] for r in rows)), "unique_shopify_ids": len(ids),
        "existing_shopify_products": len(by_id), "missing": [r for r in rows if not r["shopify_exists"]],
        "supplier_mismatches": [r for r in rows if r["shopify_exists"] and not r["supplier_matches"]]}, indent=2))


if __name__ == "__main__":
    main()
