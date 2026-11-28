"""Store-scoped admin links and read-only verification of flagged products."""
import re
import time
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from .config import get_settings

router = APIRouter()
_cache = {}


def product_id(value):
    value = str(value or "").strip()
    prefix = "gid://shopify/Product/"
    if value.startswith(prefix):
        value = value[len(prefix):]
    return value if re.fullmatch(r"[1-9][0-9]*", value) else None


def shopify_product_url(value, store=None):
    ident = product_id(value)
    domain = (get_settings().SHOPIFY_STORE if store is None else store).strip().lower()
    domain = re.sub(r"^https?://", "", domain).rstrip("/")
    if domain.endswith(".myshopify.com"):
        domain = domain[:-len(".myshopify.com")]
    if not ident or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", domain):
        return None
    return f"https://{domain}.myshopify.com/admin/products/{ident}"


@router.get("/shopify/products/{ident}/open")
def open_shopify_product(ident: str):
    url = shopify_product_url(ident)
    if not url:
        raise HTTPException(400, "Invalid Shopify product ID or store configuration")
    return RedirectResponse(url, status_code=307)


def verify_flagged_shopify_links(rows):
    """Never modify mappings or infer a replacement from a similar title."""
    from .shopify import _graphql
    store = get_settings().SHOPIFY_STORE
    now = time.monotonic()
    ids = list(dict.fromkeys(product_id(row.get("shopify_product_id")) for row in rows))
    for key in list(_cache):
        entry = _cache.get(key)
        if entry and entry[0] <= now:
            _cache.pop(key, None)
    pending = [ident for ident in ids if ident and (store, ident) not in _cache]
    for offset in range(0, len(pending), 50):
        batch = pending[offset:offset+50]
        try:
            data = _graphql('''query FlaggedProducts($ids: [ID!]!) {
              nodes(ids: $ids) { ... on Product { id title
                supplier: metafield(namespace: "aliexpress", key: "product_id") { value }
              } }
            }''', {"ids": [f"gid://shopify/Product/{ident}" for ident in batch]})
            nodes = data.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != len(batch):
                raise ValueError("Incomplete Shopify verification")
            for ident, node in zip(batch, nodes):
                if node is not None and product_id(node.get("id")) != ident:
                    raise ValueError("Unexpected Shopify product identity")
            for ident, node in zip(batch, nodes):
                _cache[(store, ident)] = (now + 60, node)
        except Exception:
            # An outage must never be reported as a deleted product.
            for ident in batch:
                _cache.pop((store, ident), None)
    for row in rows:
        ident = product_id(row.get("shopify_product_id"))
        cached = _cache.get((store, ident)) if ident else None
        row["shopify_link_status"] = "unverified"
        row["shopify_url"] = shopify_product_url(ident)
        if not ident:
            row.update(shopify_link_status="unlinked", shopify_url=None)
        elif cached:
            node = cached[1]
            if node is None:
                row.update(shopify_link_status="missing", shopify_url=None)
            else:
                supplier = (node.get("supplier") or {}).get("value")
                row["shopify_supplier_id"] = supplier
                row["shopify_link_status"] = "verified"
                if supplier and str(supplier) != str(row.get("aliexpress_id")):
                    row["shopify_link_status"] = "supplier_mismatch"
                if not row.get("title"):
                    row["title"] = node.get("title")
    return rows
