"""
shopify.py – complete with all functions needed by main.py.
"""

import re
import time
import requests
from fastapi import HTTPException
from .config import get_settings

settings = get_settings()

_cached_token = None
_token_expires_at = 0

# ─────────────────────────────────────────────
# TOKEN
# ─────────────────────────────────────────────
def _get_shopify_token():
    global _cached_token, _token_expires_at
    if _cached_token and time.time() < _token_expires_at - 60:
        return _cached_token
    if not settings.SHOPIFY_CLIENT_ID or not settings.SHOPIFY_CLIENT_SECRET:
        raise HTTPException(500, "SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET missing")
    shop = settings.SHOPIFY_STORE.replace(".myshopify.com", "").strip()
    try:
        res = requests.post(
            f"https://{shop}.myshopify.com/admin/oauth/access_token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": settings.SHOPIFY_CLIENT_ID,
                "client_secret": settings.SHOPIFY_CLIENT_SECRET,
            },
            timeout=15,
        )
        res.raise_for_status()
        body = res.json()
        _cached_token = body["access_token"]
        _token_expires_at = time.time() + body.get("expires_in", 86399)
        return _cached_token
    except Exception as e:
        raise HTTPException(502, f"Shopify token error: {e}")

def get_shopify_token():
    return _get_shopify_token()

def _base():
    shop = settings.SHOPIFY_STORE.replace(".myshopify.com", "").strip()
    return f"https://{shop}.myshopify.com/admin/api/{settings.SHOPIFY_API_VERSION}"

def _h():
    return {"X-Shopify-Access-Token": _get_shopify_token(), "Content-Type": "application/json"}

# ─────────────────────────────────────────────
# EXISTENCE & LOOKUP
# ─────────────────────────────────────────────
def check_product_exists_in_shopify(title: str) -> bool:
    if not settings.SHOPIFY_STORE:
        return False
    try:
        res = requests.get(
            f"{_base()}/products.json",
            params={"title": title, "limit": 1, "fields": "id,title"},
            headers=_h(), timeout=15,
        )
        res.raise_for_status()
        return len(res.json().get("products", [])) > 0
    except Exception:
        return False

def get_shopify_product_by_aliexpress_id(aliexpress_id: str) -> dict | None:
    if not settings.SHOPIFY_STORE:
        return None
    try:
        res = requests.get(
            f"{_base()}/products.json",
            params={
                "fields": "id,title,metafields",
                "metafield[namespace]": "aliexpress",
                "metafield[key]": "product_id",
                "metafield[value]": aliexpress_id
            },
            headers=_h(), timeout=15,
        )
        res.raise_for_status()
        products = res.json().get("products", [])
        return products[0] if products else None
    except Exception:
        return None

def get_all_shopify_imported_products() -> list:
    if not settings.SHOPIFY_STORE:
        return []
    products = []
    url = f"{_base()}/products.json?limit=250&fields=id,title,tags,status,metafields"
    token = _get_shopify_token()
    while url:
        try:
            res = requests.get(url, headers={"X-Shopify-Access-Token": token})
            res.raise_for_status()
            data = res.json()
            for p in data.get("products", []):
                if "aliexpress-import" in p.get("tags", ""):
                    ae_id = None
                    for mf in p.get("metafields", []):
                        if mf.get("namespace") == "aliexpress" and mf.get("key") == "product_id":
                            ae_id = mf.get("value")
                            break
                    products.append({
                        "shopify_id": str(p["id"]),
                        "title": p["title"],
                        "aliexpress_id": ae_id,
                        "status": p.get("status", "draft")
                    })
            link_header = res.headers.get("Link", "")
            next_match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
            url = next_match.group(1) if next_match else None
        except Exception as e:
            print(f"[Shopify] Fetch error: {e}")
            break
    return products

# ─────────────────────────────────────────────
# RATING HELPERS (minimal – optional)
# ─────────────────────────────────────────────
def _upsert_metafield(shopify_id: str, namespace: str, key: str, value: str, mf_type: str) -> bool:
    try:
        res = requests.get(
            f"{_base()}/products/{shopify_id}/metafields.json",
            params={"namespace": namespace, "key": key},
            headers=_h(), timeout=15,
        )
        res.raise_for_status()
        existing = res.json().get("metafields", [])
        payload = {"metafield": {"namespace": namespace, "key": key, "value": value, "type": mf_type}}
        if existing:
            mf_id = existing[0]["id"]
            r2 = requests.put(f"{_base()}/metafields/{mf_id}.json", json=payload, headers=_h())
        else:
            r2 = requests.post(f"{_base()}/products/{shopify_id}/metafields.json", json=payload, headers=_h())
        r2.raise_for_status()
        return True
    except Exception as e:
        print(f"[Shopify] Metafield error: {e}")
        return False

def save_rating_to_shopify(shopify_id: str, rating: str) -> dict:
    if not shopify_id or not rating:
        return {"metafield": False, "tag": False, "description": False}
    r = str(rating).strip()
    rating_json = f'{{"scale_min":"1.0","scale_max":"5.0","value":"{r}"}}'
    mf_ok = _upsert_metafield(shopify_id, "reviews", "rating", rating_json, "rating")
    _upsert_metafield(shopify_id, "custom", "rating", r, "number_decimal")
    # Tag and description methods omitted for brevity (they are optional)
    return {"metafield": mf_ok, "tag": False, "description": False}

# ─────────────────────────────────────────────
# NORMALIZER & CREATE
# ─────────────────────────────────────────────
def normalize_aliexpress_product(product: dict) -> dict:
    title = product.get("title") or "AliExpress Product"
    base_price = str(product.get("sale_price") or product.get("original_price") or "0")
    rating = str(product.get("avg_rating") or "").strip()
    product_id = product.get("product_id", "")
    product_url = product.get("product_url", "")
    body_html = f"<p>Imported from AliExpress.</p>\n"
    if rating:
        body_html += f'<p><strong>⭐ AliExpress Rating: {rating} / 5</strong></p>\n'
    body_html += f"<p>Product ID: {product_id}</p>\n<p><a href='{product_url}' target='_blank'>View on AliExpress</a></p>"
    tags = ["aliexpress-import"]
    if rating:
        tags.append(f"rating:{rating}")
    skus = product.get("skus") or []
    variants = []
    for sku in skus:
        sku_price = str(sku.get("sale_price") or sku.get("price") or base_price)
        sku_attr = sku.get("sku_attr") or ""
        option_parts = [p.split(":")[-1].strip() for p in sku_attr.split(";") if ":" in p]
        option_value = " / ".join(option_parts) if option_parts else None
        v = {"price": sku_price, "inventory_management": "shopify", "inventory_quantity": int(sku.get("stock") or 0)}
        if option_value:
            v["option1"] = option_value
        variants.append(v)
    if not variants:
        variants = [{"price": base_price}]
    payload = {
        "title": title,
        "body_html": body_html,
        "vendor": product.get("store_name") or "AliExpress",
        "product_type": "",
        "status": "draft",
        "tags": ", ".join(tags),
        "variants": variants,
        "metafields": [
            {"namespace": "aliexpress", "key": "product_id", "value": product_id, "type": "single_line_text_field"}
        ]
    }
    if any(v.get("option1") for v in variants):
        payload["options"] = [{"name": "Variant"}]
    imgs = []
    main = product.get("main_image")
    if main:
        imgs.append(main)
    for img in product.get("all_images") or []:
        if img and img not in imgs:
            imgs.append(img)
    if imgs:
        payload["images"] = [{"src": u} for u in imgs]
    return payload

# def create_shopify_product(product: dict) -> dict:
#     if not settings.SHOPIFY_STORE:
#         raise HTTPException(500, "SHOPIFY_STORE missing")
#     title = product.get("title") or "AliExpress Product"
#     if check_product_exists_in_shopify(title):
#         raise HTTPException(409, f"Product '{title}' already exists in Shopify.")
#     try:
#         res = requests.post(f"{_base()}/products.json", json={"product": normalize_aliexpress_product(product)}, headers=_h(), timeout=30)
#         res.raise_for_status()
#         return res.json()
#     except Exception as e:
#         detail = getattr(e.response, "text", str(e)) if hasattr(e, "response") else str(e)
#         raise HTTPException(502, f"Shopify create error: {detail}")

def create_shopify_product(product: dict) -> dict:
    if not settings.SHOPIFY_STORE:
        raise HTTPException(500, "SHOPIFY_STORE missing")
    title = product.get("title") or "AliExpress Product"
    if check_product_exists_in_shopify(title):
        raise HTTPException(409, f"Product '{title}' already exists in Shopify.")
    try:
        payload = normalize_aliexpress_product(product)
        res = requests.post(f"{_base()}/products.json", json={"product": payload}, headers=_h(), timeout=30)
        res.raise_for_status()
        shopify_data = res.json()
        shopify_product = shopify_data["product"]
        
        # Store SKU IDs for variant price sync
        skus = product.get("skus", [])
        if skus:
            store_aliexpress_sku_ids(shopify_product["id"], skus)
        
        return shopify_data
    except Exception as e:
        detail = getattr(e.response, "text", str(e)) if hasattr(e, "response") else str(e)
        raise HTTPException(502, f"Shopify create error: {detail}")

# ─────────────────────────────────────────────
# UPDATE FUNCTIONS
# ─────────────────────────────────────────────
def update_shopify_product(shopify_product_id: str, updates: dict):
    if not settings.SHOPIFY_STORE:
        raise HTTPException(500, "SHOPIFY_STORE missing")
    payload = {}
    if updates.get("title"):
        payload["title"] = updates["title"]
    if updates.get("price"):
        payload["variants"] = [{"price": str(updates["price"])}]
    if updates.get("body_html"):
        payload["body_html"] = updates["body_html"]
    if payload:
        try:
            res = requests.put(f"{_base()}/products/{shopify_product_id}.json", json={"product": payload}, headers=_h(), timeout=30)
            res.raise_for_status()
        except Exception as e:
            detail = getattr(e.response, "text", str(e)) if hasattr(e, "response") else str(e)
            raise HTTPException(502, f"Shopify update failed: {detail}")
    rating = str(updates.get("rating") or "").strip()
    if rating:
        save_rating_to_shopify(shopify_product_id, rating)

def update_shopify_product_price(shopify_product_id: str, new_price: float) -> bool:
    if not settings.SHOPIFY_STORE:
        return False
    try:
        res = requests.get(f"{_base()}/products/{shopify_product_id}.json", params={"fields": "id,variants"}, headers=_h(), timeout=15)
        res.raise_for_status()
        variants = res.json().get("product", {}).get("variants", [])
        if not variants:
            return False
        updated_variants = [{"id": v["id"], "price": str(new_price)} for v in variants]
        r2 = requests.put(f"{_base()}/products/{shopify_product_id}.json", json={"product": {"variants": updated_variants}}, headers=_h(), timeout=30)
        r2.raise_for_status()
        print(f"[Shopify] Price updated for {shopify_product_id} to {new_price}")
        return True
    except Exception as e:
        print(f"[Shopify] Price update failed: {e}")
        return False

def update_shopify_product_prices_with_skus(shopify_product_id: str, aliexpress_skus: list) -> bool:
    if not settings.SHOPIFY_STORE:
        return False
    print(f"\n[UPDATE] Starting variant price sync for product {shopify_product_id}")
    price_by_ae_sku = {}
    for sku in aliexpress_skus:
        ae_sku_id = str(sku.get("sku_id"))
        price = sku.get("sale_price") or sku.get("price")
        if ae_sku_id and price:
            price_by_ae_sku[ae_sku_id] = float(price)
    if not price_by_ae_sku:
        print("[UPDATE] No AE SKU IDs")
        return False
    try:
        res = requests.get(f"{_base()}/products/{shopify_product_id}.json", params={"fields": "id,variants"}, headers=_h(), timeout=15)
        res.raise_for_status()
        shopify_variants = res.json().get("product", {}).get("variants", [])
        if not shopify_variants:
            return False
    except Exception as e:
        print(f"[UPDATE] Fetch error: {e}")
        return False
    variant_ae_map = {}
    for variant in shopify_variants:
        vid = variant["id"]
        try:
            mf_res = requests.get(f"{_base()}/variants/{vid}/metafields.json", params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(), timeout=10)
            if mf_res.status_code == 200:
                mfs = mf_res.json().get("metafields", [])
                if mfs:
                    variant_ae_map[vid] = mfs[0].get("value")
        except Exception as e:
            print(f"[UPDATE] Metafield error for variant {vid}: {e}")
    updated_variants = []
    changes = False
    for variant in shopify_variants:
        new_price = None
        ae_sku_id = variant_ae_map.get(variant["id"])
        if ae_sku_id and ae_sku_id in price_by_ae_sku:
            new_price = price_by_ae_sku[ae_sku_id]
        if new_price is not None and abs(float(variant["price"]) - new_price) > 0.01:
            var_copy = variant.copy()
            var_copy["price"] = str(new_price)
            updated_variants.append(var_copy)
            changes = True
            print(f"[UPDATE] {variant.get('option1', '?')}: {variant['price']} → {new_price}")
        else:
            updated_variants.append(variant.copy())
    if not changes:
        print("[UPDATE] No price changes")
        return True
    try:
        r2 = requests.put(f"{_base()}/products/{shopify_product_id}.json", json={"product": {"variants": updated_variants}}, headers=_h(), timeout=30)
        r2.raise_for_status()
        print(f"[UPDATE] Success, {len(updated_variants)} variants updated")
        return True
    except Exception as e:
        print(f"[UPDATE] Update failed: {e}")
        return False

def store_aliexpress_sku_ids(shopify_product_id: str, aliexpress_skus: list):
    res = requests.get(f"{_base()}/products/{shopify_product_id}.json", params={"fields": "id,variants"}, headers=_h())
    res.raise_for_status()
    variants = res.json()["product"]["variants"]
    for i, variant in enumerate(variants):
        if i < len(aliexpress_skus):
            sku_id = str(aliexpress_skus[i].get("sku_id"))
            mf_url = f"{_base()}/variants/{variant['id']}/metafields.json"
            check = requests.get(mf_url, params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h())
            existing = check.json().get("metafields", [])
            payload = {"metafield": {"namespace": "aliexpress", "key": "sku_id", "value": sku_id, "type": "single_line_text_field"}}
            if existing:
                mf_id = existing[0]["id"]
                requests.put(f"{_base()}/metafields/{mf_id}.json", json=payload, headers=_h())
            else:
                requests.post(mf_url, json=payload, headers=_h())
    print(f"[Shopify] Stored SKU IDs for {len(aliexpress_skus)} variants")


def increase_shopify_product_price(shopify_product_id: str, increase_by: float) -> bool:
    """Increase all variants of a Shopify product by a fixed amount."""
    if not settings.SHOPIFY_STORE:
        return False
    try:
        # Fetch current variants
        res = requests.get(
            f"{_base()}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"},
            headers=_h(),
            timeout=15,
        )
        res.raise_for_status()
        product_data = res.json().get("product", {})
        variants = product_data.get("variants", [])
        if not variants:
            print(f"[Shopify] No variants found for product {shopify_product_id}")
            return False

        # Calculate new prices
        updated_variants = []
        for variant in variants:
            try:
                current_price = float(variant["price"])
            except (ValueError, TypeError):
                current_price = 0.0
            new_price = current_price + increase_by
            updated_variants.append({
                "id": variant["id"],
                "price": f"{new_price:.2f}"
            })

        # Send update
        update_payload = {"product": {"variants": updated_variants}}
        r2 = requests.put(
            f"{_base()}/products/{shopify_product_id}.json",
            json=update_payload,
            headers=_h(),
            timeout=30
        )
        r2.raise_for_status()
        print(f"[Shopify] Increased {len(updated_variants)} variants by {increase_by} for product {shopify_product_id}")
        return True
    except Exception as e:
        print(f"[Shopify] Price increase failed: {e}")
        return False