"""
shopify.py – complete with all functions needed by main.py.
Now includes per-variant SKU image upload via Shopify Images API.
"""

import re
import time
import requests
from fastapi import HTTPException
from .config import get_settings
import time as _time

import threading

settings = get_settings()

_cached_token = None
_token_expires_at = 0

_rate_lock = threading.Lock()
_last_call_time = 0.0
MIN_INTERVAL = 0.55  # ~1.8 req/sec, safely under Shopify's 2/sec sustained limit


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
# RATING HELPERS
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
    return {"metafield": mf_ok, "tag": False, "description": False}

# ─────────────────────────────────────────────
# SKU IMAGE HELPERS
# ─────────────────────────────────────────────

def _upload_image_to_shopify(shopify_product_id: str, image_url: str, alt: str = "") -> int | None:
    """
    Upload an image URL to a Shopify product.
    Returns the new Shopify image ID, or None on failure.
    """
    try:
        payload = {"image": {"src": image_url, "alt": alt}}
        res = requests.post(
            f"{_base()}/products/{shopify_product_id}/images.json",
            json=payload,
            headers=_h(),
            timeout=30,
        )
        res.raise_for_status()
        return res.json().get("image", {}).get("id")
    except Exception as e:
        print(f"[Shopify][Image] Upload failed for {image_url}: {e}")
        return None



# def attach_sku_images_to_product(shopify_product_id: str, aliexpress_skus: list, shopify_variants: list) -> int:
#     if not aliexpress_skus or not shopify_variants:
#         return 0

#     locked_variant_ids = get_locked_variant_ids(shopify_product_id, "image")
#     url_to_image_id: dict[str, int] = {}
#     attached = 0

#     for i, shopify_variant in enumerate(shopify_variants):
#         if shopify_variant["id"] in locked_variant_ids:
#             continue
#         if i >= len(aliexpress_skus):
#             break

#         ae_sku = aliexpress_skus[i]
#         img_url = ae_sku.get("image")
#         if not img_url:
#             continue

#         if img_url not in url_to_image_id:
#             image_id = _upload_image_to_shopify(shopify_product_id, img_url, alt=ae_sku.get("label", ""))
#             if image_id:
#                 url_to_image_id[img_url] = image_id
#             else:
#                 continue
#         else:
#             image_id = url_to_image_id[img_url]

#         variant_id = shopify_variant["id"]
#         try:
#             res = requests.put(
#                 f"{_base()}/variants/{variant_id}.json",
#                 json={"variant": {"id": variant_id, "image_id": image_id}},
#                 headers=_h(),
#                 timeout=15,
#             )
#             res.raise_for_status()
#             attached += 1
#             print(f"[Shopify][Image] Variant {variant_id} ← image {image_id} ({ae_sku.get('label','')})")
#         except Exception as e:
#             print(f"[Shopify][Image] Variant link failed for {variant_id}: {e}")

#     return attached



def attach_sku_images_to_product(shopify_product_id: str, aliexpress_skus: list, shopify_variants: list) -> int:
    if not aliexpress_skus or not shopify_variants:
        return 0

    locked_variant_ids = get_locked_variant_ids(shopify_product_id, "image")

    # Build sku_id -> ae_sku lookup instead of relying on array position
    ae_sku_by_id = {str(s.get("sku_id")): s for s in aliexpress_skus if s.get("sku_id")}

    # Fetch each variant's aliexpress.sku_id metafield (same source of truth
    # your price/inventory sync already relies on)
    variant_ae_map = {}
    for variant in shopify_variants:
        vid = variant["id"]
        try:
            mf_res = _shopify_request(
                "GET", f"{_base()}/variants/{vid}/metafields.json",
                params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(),
            )
            if mf_res.status_code == 200:
                mfs = mf_res.json().get("metafields", [])
                if mfs:
                    variant_ae_map[vid] = mfs[0].get("value")
        except Exception as e:
            print(f"[Shopify][Image] Metafield lookup failed for variant {vid}: {e}")

    url_to_image_id: dict[str, int] = {}
    attached = 0
    unmatched = []

    for i, shopify_variant in enumerate(shopify_variants):
        variant_id = shopify_variant["id"]
        if variant_id in locked_variant_ids:
            continue

        # 1. Match by this variant's own sku_id metafield — reliable, order-independent
        ae_sku_id = variant_ae_map.get(variant_id)
        ae_sku = ae_sku_by_id.get(ae_sku_id) if ae_sku_id else None

        # 2. Fall back to positional match only if no metafield exists at all
        #    (e.g. product imported before metafields were being stored)
        if ae_sku is None and i < len(aliexpress_skus):
            ae_sku = aliexpress_skus[i]

        if ae_sku is None:
            unmatched.append(variant_id)
            continue

        img_url = ae_sku.get("image")
        if not img_url:
            continue  # this SKU genuinely has no image on AliExpress's side

        if img_url not in url_to_image_id:
            image_id = _upload_image_to_shopify(
                shopify_product_id, img_url, alt=ae_sku.get("label", ""),
            )
            if image_id:
                url_to_image_id[img_url] = image_id
            else:
                continue
        else:
            image_id = url_to_image_id[img_url]

        try:
            res = requests.put(
                f"{_base()}/variants/{variant_id}.json",
                json={"variant": {"id": variant_id, "image_id": image_id}},
                headers=_h(),
                timeout=15,
            )
            res.raise_for_status()
            attached += 1
            print(f"[Shopify][Image] Variant {variant_id} ← image {image_id} ({ae_sku.get('label','')})")
        except Exception as e:
            print(f"[Shopify][Image] Variant link failed for {variant_id}: {e}")

    if unmatched:
        print(f"[Shopify][Image] {len(unmatched)} variant(s) had no matching AliExpress SKU at all: {unmatched}")

    return attached



# def backfill_sku_images(shopify_product_id: str, aliexpress_skus: list) -> dict:
#     """
#     Fetch current Shopify variants, then attach any missing SKU images.
#     Skips variants that already have an image_id.
#     Returns {"attached": int, "skipped": int, "total_variants": int}
#     """
#     try:
#         res = requests.get(
#             f"{_base()}/products/{shopify_product_id}.json",
#             params={"fields": "id,variants"},
#             headers=_h(), timeout=15,
#         )
#         res.raise_for_status()
#         shopify_variants = res.json().get("product", {}).get("variants", [])
#     except Exception as e:
#         print(f"[Backfill][Image] Fetch variants failed: {e}")
#         return {"attached": 0, "skipped": 0, "total_variants": 0}

#     # Only process variants that don't have an image yet
#     needs_image = [v for v in shopify_variants if not v.get("image_id")]
#     already_has = len(shopify_variants) - len(needs_image)

#     if not needs_image:
#         return {"attached": 0, "skipped": already_has, "total_variants": len(shopify_variants)}

#     # Build matching sku list for the ones that need images
#     # We map by position — same assumption as store_aliexpress_sku_ids
#     url_to_image_id: dict[str, int] = {}
#     attached = 0

#     for i, shopify_variant in enumerate(shopify_variants):
#         if shopify_variant.get("image_id"):
#             continue  # already has one
#         if i >= len(aliexpress_skus):
#             break

#         ae_sku = aliexpress_skus[i]
#         img_url = ae_sku.get("image")
#         if not img_url:
#             continue

#         if img_url not in url_to_image_id:
#             image_id = _upload_image_to_shopify(
#                 shopify_product_id,
#                 img_url,
#                 alt=ae_sku.get("label", ""),
#             )
#             if image_id:
#                 url_to_image_id[img_url] = image_id
#             else:
#                 continue
#         else:
#             image_id = url_to_image_id[img_url]

#         variant_id = shopify_variant["id"]
#         try:
#             res2 = requests.put(
#                 f"{_base()}/variants/{variant_id}.json",
#                 json={"variant": {"id": variant_id, "image_id": image_id}},
#                 headers=_h(), timeout=15,
#             )
#             res2.raise_for_status()
#             attached += 1
#             print(f"[Backfill][Image] Variant {variant_id} ← image {image_id}")
#         except Exception as e:
#             print(f"[Backfill][Image] Link failed {variant_id}: {e}")

#     return {
#         "attached": attached,
#         "skipped": already_has,
#         "total_variants": len(shopify_variants),
#     }



def _get_all_variants_for_image_sync(shopify_product_id: str) -> list:
    """Return every variant, including products with more than 100 variants."""
    query = """
    query ImageSyncVariants($id: ID!, $after: String) {
      product(id: $id) {
        variants(first: 100, after: $after) {
          nodes {
            id
            image { id }
            skuId: metafield(namespace: "aliexpress", key: "sku_id") { value }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    variants = []
    after = None

    while True:
        data = _graphql(query, {
            "id": _product_gid(shopify_product_id),
            "after": after,
        })
        product = data.get("product")
        if not product:
            raise RuntimeError(f"Shopify product {shopify_product_id} was not found")

        connection = product.get("variants") or {}
        for node in connection.get("nodes", []):
            image = node.get("image") or {}
            sku_metafield = node.get("skuId") or {}
            variants.append({
                "id": int(_numeric_id_from_gid(node["id"])),
                "image_id": (
                    int(_numeric_id_from_gid(image["id"]))
                    if image.get("id") else None
                ),
                "aliexpress_sku_id": (
                    str(sku_metafield["value"])
                    if sku_metafield.get("value") is not None else None
                ),
            })

        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            raise RuntimeError("Shopify variant pagination returned no end cursor")

    return variants


def backfill_sku_images(shopify_product_id: str, aliexpress_skus: list) -> dict:
    try:
        shopify_variants = _get_all_variants_for_image_sync(shopify_product_id)
    except Exception as e:
        print(f"[Backfill][Image] Fetch variants failed: {e}")
        return {"attached": 0, "skipped": 0, "total_variants": 0}

    locked_variant_ids = get_locked_variant_ids(shopify_product_id, "image")

    # Skip variants that already have an image OR are locked
    needs_image = [
        v for v in shopify_variants
        if not v.get("image_id") and v["id"] not in locked_variant_ids
    ]
    already_has = len(shopify_variants) - len(needs_image)

    if not needs_image:
        return {"attached": 0, "skipped": already_has, "total_variants": len(shopify_variants)}

    url_to_image_id: dict[str, int] = {}
    ae_sku_by_id = {
        str(sku.get("sku_id")): sku
        for sku in aliexpress_skus
        if sku.get("sku_id") is not None
    }
    attached = 0
    unmatched = 0

    for i, shopify_variant in enumerate(shopify_variants):
        if shopify_variant.get("image_id"):
            continue
        if shopify_variant["id"] in locked_variant_ids:
            continue  # manually customized — skip
        # Current imports store the AliExpress SKU ID on each Shopify variant.
        # Keep positional matching only for products imported before that
        # metafield existed.
        ae_sku_id = shopify_variant.get("aliexpress_sku_id")
        if ae_sku_id is not None:
            ae_sku = ae_sku_by_id.get(ae_sku_id)
        else:
            ae_sku = aliexpress_skus[i] if i < len(aliexpress_skus) else None
        if ae_sku is None:
            unmatched += 1
            continue

        img_url = ae_sku.get("image")
        if not img_url:
            continue

        if img_url not in url_to_image_id:
            image_id = _upload_image_to_shopify(shopify_product_id, img_url, alt=ae_sku.get("label", ""))
            if image_id:
                url_to_image_id[img_url] = image_id
            else:
                continue
        else:
            image_id = url_to_image_id[img_url]

        variant_id = shopify_variant["id"]
        try:
            res2 = requests.put(
                f"{_base()}/variants/{variant_id}.json",
                json={"variant": {"id": variant_id, "image_id": image_id}},
                headers=_h(), timeout=15,
            )
            res2.raise_for_status()
            attached += 1
            print(f"[Backfill][Image] Variant {variant_id} <- image {image_id}")
        except Exception as e:
            print(f"[Backfill][Image] Link failed {variant_id}: {e}")

    if unmatched:
        print(f"[Backfill][Image] {unmatched} variant(s) had no matching AliExpress SKU")

    return {
        "attached": attached,
        "skipped": already_has,
        "total_variants": len(shopify_variants),
    }


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
        #"vendor": product.get("store_name") or "AliExpress",
        "vendor": "UGNE",
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
        shopify_product_id = str(shopify_product["id"])
        shopify_variants   = shopify_product.get("variants", [])

        # ── Store AliExpress SKU ID metafields ──
        skus = product.get("skus", [])
        if skus:
            store_aliexpress_sku_ids(shopify_product_id, skus)

        # ── Upload per-variant SKU images ──
        if skus and shopify_variants:
            attached = attach_sku_images_to_product(shopify_product_id, skus, shopify_variants)
            print(f"[Shopify] Attached {attached}/{len(shopify_variants)} SKU images for product {shopify_product_id}")

        return shopify_data
    except HTTPException:
        raise
    except Exception as e:
        detail = getattr(e, "response", None)
        detail = detail.text if detail else str(e)
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
    if updates.get("body_html"):
        payload["body_html"] = updates["body_html"]

    if updates.get("price"):
        try:
            res = requests.get(
                f"{_base()}/products/{shopify_product_id}.json",
                params={"fields": "id,variants"},
                headers=_h(), timeout=15,
            )
            res.raise_for_status()
            existing_variants = res.json().get("product", {}).get("variants", [])

            for v in existing_variants:
                try:
                    r_v = requests.put(
                        f"{_base()}/variants/{v['id']}.json",
                        json={"variant": {"id": v["id"], "price": str(updates["price"])}},
                        headers=_h(), timeout=20,
                    )
                    r_v.raise_for_status()
                except Exception as e:
                    print(f"[Shopify] Variant price update failed for {v['id']}: {e}")

        except Exception as e:
            detail = getattr(e, "response", None)
            detail = detail.text if detail else str(e)
            raise HTTPException(502, f"Failed to fetch existing variants: {detail}")

    if payload:
        try:
            res = requests.put(
                f"{_base()}/products/{shopify_product_id}.json",
                json={"product": payload},
                headers=_h(), timeout=30,
            )
            res.raise_for_status()
        except Exception as e:
            detail = getattr(e, "response", None)
            detail = detail.text if detail else str(e)
            raise HTTPException(502, f"Shopify update failed: {detail}")

    rating = str(updates.get("rating") or "").strip()
    if rating:
        save_rating_to_shopify(shopify_product_id, rating)


def update_shopify_product_price(shopify_product_id: str, new_price: float) -> bool:
    if not settings.SHOPIFY_STORE:
        return False
    try:
        res = requests.get(
            f"{_base()}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"},
            headers=_h(), timeout=15,
        )
        res.raise_for_status()
        variants = res.json().get("product", {}).get("variants", [])
        if not variants:
            return False

        success_count = 0
        for v in variants:
            try:
                r2 = requests.put(
                    f"{_base()}/variants/{v['id']}.json",
                    json={"variant": {"id": v["id"], "price": str(new_price)}},
                    headers=_h(), timeout=20,
                )
                r2.raise_for_status()
                success_count += 1
            except Exception as e:
                print(f"[Shopify] Price update failed for variant {v['id']}: {e}")

        print(f"[Shopify] Price updated for {success_count}/{len(variants)} variant(s) of {shopify_product_id} to {new_price}")
        return success_count > 0
    except Exception as e:
        print(f"[Shopify] Price update failed: {e}")
        return False

# def update_shopify_product_prices_with_skus(shopify_product_id: str, aliexpress_skus: list) -> str:
#     """
#     Returns one of: "updated", "unchanged", "failed"
#     """
#     if not settings.SHOPIFY_STORE:
#         return "failed"
#     print(f"\n[UPDATE] Starting variant price sync for product {shopify_product_id}")

#     price_by_ae_sku = {}
#     for sku in aliexpress_skus:
#         ae_sku_id = str(sku.get("sku_id"))
#         price = sku.get("sale_price") or sku.get("price")
#         if ae_sku_id and price:
#             price_by_ae_sku[ae_sku_id] = float(price)

#     if not price_by_ae_sku:
#         print("[UPDATE] No AE SKU IDs")
#         return "failed"

#     try:
#         res = requests.get(
#             f"{_base()}/products/{shopify_product_id}.json",
#             params={"fields": "id,variants"},
#             headers=_h(), timeout=15,
#         )
#         res.raise_for_status()
#         shopify_variants = res.json().get("product", {}).get("variants", [])
#         if not shopify_variants:
#             return "failed"
#     except Exception as e:
#         print(f"[UPDATE] Fetch error: {e}")
#         return "failed"

#     variant_ae_map = {}
#     for variant in shopify_variants:
#         vid = variant["id"]
#         try:
#             mf_res = requests.get(
#                 f"{_base()}/variants/{vid}/metafields.json",
#                 params={"namespace": "aliexpress", "key": "sku_id"},
#                 headers=_h(), timeout=10,
#             )
#             if mf_res.status_code == 200:
#                 mfs = mf_res.json().get("metafields", [])
#                 if mfs:
#                     variant_ae_map[vid] = mfs[0].get("value")
#         except Exception as e:
#             print(f"[UPDATE] Metafield error for variant {vid}: {e}")

#     matched_via_metafield = any(v["id"] in variant_ae_map for v in shopify_variants)

#     price_by_label = {}
#     for sku in aliexpress_skus:
#         label = (sku.get("label") or sku.get("sku_attr") or "").strip().lower()
#         price = sku.get("sale_price") or sku.get("price")
#         if label and price:
#             price_by_label[label] = float(price)

#     def _variant_label(variant: dict) -> str:
#         parts = [
#             variant.get(f"option{i}")
#             for i in (1, 2, 3)
#             if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
#         ]
#         return " / ".join(parts).strip().lower()

#     def _fuzzy_label_match(variant_label: str):
#         if not variant_label:
#             return None
#         if variant_label in price_by_label:
#             return price_by_label[variant_label]
#         variant_tokens = set(t.strip() for t in variant_label.split("/"))
#         for label, price in price_by_label.items():
#             label_tokens = set(t.strip() for t in label.split("/"))
#             if variant_tokens and variant_tokens.issubset(label_tokens):
#                 return price
#         return None

#     print(f"[UPDATE][DEBUG] price_by_ae_sku = {price_by_ae_sku}")
#     print(f"[UPDATE][DEBUG] price_by_label = {price_by_label}")
#     print(f"[UPDATE][DEBUG] matched_via_metafield = {matched_via_metafield}")

#     updated_variants = []
#     changes = False
#     any_match_found = False
#     for variant in shopify_variants:
#         new_price = None
#         ae_sku_id = variant_ae_map.get(variant["id"])

#         if ae_sku_id and ae_sku_id in price_by_ae_sku:
#             new_price = price_by_ae_sku[ae_sku_id]
#         elif not matched_via_metafield:
#             label = _variant_label(variant)
#             new_price = _fuzzy_label_match(label)
#             if new_price is None and len(price_by_ae_sku) == 1:
#                 new_price = next(iter(price_by_ae_sku.values()))

#         if new_price is not None:
#             any_match_found = True

#         if new_price is not None and abs(float(variant["price"]) - new_price) > 0.01:
#             var_copy = variant.copy()
#             var_copy["price"] = str(new_price)
#             updated_variants.append(var_copy)
#             changes = True
#             print(f"[UPDATE] {variant.get('option1', '?')}: {variant['price']} → {new_price}")
#         else:
#             updated_variants.append(variant.copy())

#     if not any_match_found:
#         print("[UPDATE] No price changes (no metafield/label/single-SKU match found)")
#         return "failed"

#     if not changes:
#         print("[UPDATE] Price already up to date — no Shopify update needed")
#         return "unchanged"

#     try:
#         r2 = requests.put(
#             f"{_base()}/products/{shopify_product_id}.json",
#             json={"product": {"variants": updated_variants}},
#             headers=_h(), timeout=30,
#         )
#         r2.raise_for_status()
#         print(f"[UPDATE] Success, {len(updated_variants)} variants updated")
#         return "updated"
#     except Exception as e:
#         print(f"[UPDATE] Update failed: {e}")
#         return "failed"


# def update_shopify_product_prices_with_skus(shopify_product_id: str, aliexpress_skus: list) -> str:
#     """
#     Returns one of: "updated", "unchanged", "failed"
#     """
#     if not settings.SHOPIFY_STORE:
#         return "failed"
#     print(f"\n[UPDATE] Starting variant price sync for product {shopify_product_id}")

#     price_by_ae_sku = {}
#     for sku in aliexpress_skus:
#         ae_sku_id = str(sku.get("sku_id"))
#         price = sku.get("sale_price") or sku.get("price")
#         if ae_sku_id and ae_sku_id != "None" and price:
#             price_by_ae_sku[ae_sku_id] = float(price)

#     if not price_by_ae_sku and not aliexpress_skus:
#         print("[UPDATE] No AE SKU IDs")
#         return "failed"

#     try:
#         res = _shopify_request(
#             "GET", f"{_base()}/products/{shopify_product_id}.json",
#             params={"fields": "id,variants"}, headers=_h(), timeout=15,
#         )
#         res.raise_for_status()
#         shopify_variants = res.json().get("product", {}).get("variants", [])
#         if not shopify_variants:
#             return "failed"
#     except Exception as e:
#         print(f"[UPDATE] Fetch error: {e}")
#         return "failed"

#     locked_variant_ids = get_locked_variant_ids(shopify_product_id, "price")
#     if locked_variant_ids:
#         print(f"[UPDATE] {len(locked_variant_ids)} variant(s) locked — will be skipped: {locked_variant_ids}")

#     variant_ae_map = {}
#     for variant in shopify_variants:
#         vid = variant["id"]
#         try:
#             mf_res = _shopify_request(
#                 "GET", f"{_base()}/variants/{vid}/metafields.json",
#                 params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(),
#             )
#             if mf_res.status_code == 200:
#                 mfs = mf_res.json().get("metafields", [])
#                 if mfs:
#                     variant_ae_map[vid] = mfs[0].get("value")
#         except Exception as e:
#             print(f"[UPDATE] Metafield error for variant {vid}: {e}")

#     price_by_label = {}
#     for sku in aliexpress_skus:
#         label = (sku.get("label") or sku.get("sku_attr") or "").strip().lower()
#         price = sku.get("sale_price") or sku.get("price")
#         if label and price:
#             price_by_label[label] = float(price)

#     def _variant_label(variant: dict) -> str:
#         parts = [
#             variant.get(f"option{i}")
#             for i in (1, 2, 3)
#             if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
#         ]
#         return " / ".join(parts).strip().lower()

#     def _fuzzy_label_match(variant_label: str):
#         if not variant_label:
#             return None
#         if variant_label in price_by_label:
#             return price_by_label[variant_label]
#         variant_tokens = set(t.strip() for t in variant_label.split("/"))
#         for label, price in price_by_label.items():
#             label_tokens = set(t.strip() for t in label.split("/"))
#             if variant_tokens and variant_tokens.issubset(label_tokens):
#                 return price
#         return None

#     # ── Build update list — per-variant fallback runs even if OTHER variants matched via metafield ──
#     to_update = []
#     any_match_found = False
#     skipped_locked = 0

#     for i, variant in enumerate(shopify_variants):
#         if variant["id"] in locked_variant_ids:
#             skipped_locked += 1
#             continue

#         new_price = None
#         ae_sku_id = variant_ae_map.get(variant["id"])

#         if ae_sku_id and ae_sku_id in price_by_ae_sku:
#             new_price = price_by_ae_sku[ae_sku_id]
#         else:
#             label = _variant_label(variant)
#             new_price = _fuzzy_label_match(label)
#             if new_price is None and i < len(aliexpress_skus):
#                 ae_sku = aliexpress_skus[i]
#                 ae_price = ae_sku.get("sale_price") or ae_sku.get("price")
#                 if ae_price is not None:
#                     new_price = float(ae_price)

#         if new_price is not None:
#             any_match_found = True
#             if abs(float(variant["price"]) - new_price) > 0.01:
#                 to_update.append((variant["id"], new_price, variant.get("option1", "?"), variant["price"]))

#     if skipped_locked:
#         print(f"[UPDATE] Skipped {skipped_locked} price-locked variant(s) for product {shopify_product_id}")

#     if not any_match_found:
#         print("[UPDATE] No price changes (no metafield/label/positional match found)")
#         return "failed"

#     if not to_update:
#         print("[UPDATE] Price already up to date — no Shopify update needed")
#         return "unchanged"

#     # ── Per-variant PUT, throttled + auto-retried on 429 ──
#     success_count = 0
#     for variant_id, new_price, option_label, old_price in to_update:
#         try:
#             r2 = _shopify_request(
#                 "PUT", f"{_base()}/variants/{variant_id}.json",
#                 json={"variant": {"id": variant_id, "price": str(new_price)}},
#                 headers=_h(), timeout=20,
#             )
#             r2.raise_for_status()
#             success_count += 1
#             print(f"[UPDATE] {option_label}: {old_price} → {new_price}")
#         except Exception as e:
#             print(f"[UPDATE] Failed for variant {variant_id}: {e}")

#     if success_count == 0:
#         return "failed"

#     print(f"[UPDATE] Success, {success_count}/{len(to_update)} variants updated")
#     return "updated"


def _sku_label_key(label):
    # Preserve option boundaries and punctuation; normalize only presentation.
    label = str(label or "").casefold().translate(str.maketrans({
        "–": "-", "—": "-", "‑": "-", "−": "-",
    }))
    return tuple(sorted(re.sub(r"\s+", "", part) for part in label.split("/")))


def _match_supplier_variants(variants, skus):
    """Return one-to-one matches, never assigning by supplier array position.

    A unique complete option label repairs stale or previously positional links.
    Existing IDs still support custom Shopify titles. Ambiguities fail closed.
    """
    by_label, by_id = {}, {}
    for index, sku in enumerate(skus):
        # Also recognize option values produced by older imports (e.g. 29#72v lcd).
        legacy_label = " / ".join(part.split(":")[-1].strip()
                                  for part in (sku.get("sku_attr") or "").split(";") if ":" in part)
        for label in {_sku_label_key(sku.get("label")), _sku_label_key(legacy_label)}:
            if label != ("",):
                by_label.setdefault(label, []).append(index)
        if sku.get("sku_id") is not None:
            by_id.setdefault(str(sku["sku_id"]), []).append(index)
    proposed = {}
    for vid, variant in variants.items():
        label = _sku_label_key(variant.get("label"))
        candidates = by_label.get(label, [])
        id_candidates = by_id.get(str(variant.get("ae_sku_id")), [])
        if len(candidates) > 1:
            candidates = [i for i in candidates if i in id_candidates]
        elif not candidates:
            candidates = id_candidates
            if not candidates and label == ("",) and not variant.get("ae_sku_id") and len(variants) == len(skus) == 1:
                candidates = [0]
        if len(candidates) == 1:
            proposed[vid] = candidates[0]
    counts = {}
    for index in proposed.values():
        counts[index] = counts.get(index, 0) + 1
    return {vid: skus[index] for vid, index in proposed.items() if counts[index] == 1}


def update_shopify_product_prices_with_skus(shopify_product_id: str, aliexpress_skus: list) -> str:
    """Sync each unlocked variant from its own supplier price and saved increase."""
    from decimal import Decimal, ROUND_HALF_UP
    from .price_history import observe_supplier_price
    if not settings.SHOPIFY_STORE or not aliexpress_skus:
        return "failed"
    try:
        # Fresh snapshot: no stale lock cache and no separate requests per SKU.
        variants = get_variant_sync_state(shopify_product_id)
        if not variants:
            return "failed"
        matched = _match_supplier_variants(variants, aliexpress_skus)
        for sku in aliexpress_skus:
            raw = sku.get("sale_price") or sku.get("price")
            if raw is None:
                continue
            price = Decimal(str(raw))
            if not price.is_finite() or price < 0:
                raise ValueError("Invalid AliExpress variant price")

        updates, unmatched = [], []
        for vid, variant in variants.items():
            sku = matched.get(vid)
            raw = (sku.get("sale_price") or sku.get("price")) if sku else None
            if raw is None:
                if not variant["locks"]["price"]:
                    unmatched.append(vid)
                continue
            base, supplier_price = Decimal(str(raw)), sku.get("_supplier_price", raw)
            previous = variant.get("supplier_history")
            repairing_link = sku.get("sku_id") is not None and str(sku["sku_id"]) != str(variant["ae_sku_id"])
            # A repaired link may have observed a different supplier variant.
            # Establish its baseline without claiming that difference was a price move.
            history = observe_supplier_price(None if repairing_link else previous, supplier_price)
            update = {"variant_id": vid}
            if repairing_link:
                update["ae_sku_id"] = str(sku["sku_id"])
            if history != previous:
                update["supplier_history"] = history
                update["supplier_history_id"] = variant.get("supplier_history_id")
            if variant["locks"]["price"]:
                if len(update) > 1:
                    updates.append(update)
                continue
            final = (base + variant["price_increase"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if final < 0:
                raise ValueError(f"Negative final price for variant {vid}")
            if Decimal(str(variant["price"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != final:
                update["price"] = str(final)
            if len(update) > 1:
                updates.append(update)
            print(f"[PriceSync] variant={vid} supplier={base} increase={variant['price_increase']} final={final}")
        if unmatched:
            print(f"[PriceSync] No unambiguous AliExpress SKU match for variants {unmatched}")
        if not updates:
            return "failed" if unmatched else "unchanged"
        for offset in range(0, len(updates), 100):
            batch = updates[offset:offset + 100]
            result = bulk_update_variant_prices(shopify_product_id, batch)
            if not result["success"] or result.get("errors") or result["updated"] != len(batch):
                print(f"[PriceSync] Shopify update incomplete: {result}")
                return "failed"
        return "failed" if unmatched else "updated" if any("price" in row for row in updates) else "unchanged"
    except Exception as exc:
        print(f"[PriceSync] Product {shopify_product_id} failed: {exc}")
        return "failed"


def store_aliexpress_sku_ids(shopify_product_id: str, aliexpress_skus: list):
    variants = get_variant_sync_state(shopify_product_id)
    matches = _match_supplier_variants(variants, aliexpress_skus)
    updates = [{"variant_id": vid, "ae_sku_id": str(sku["sku_id"])}
               for vid, sku in matches.items() if sku.get("sku_id") is not None
               and str(sku["sku_id"]) != str(variants[vid]["ae_sku_id"])]
    for offset in range(0, len(updates), 100):
        result = bulk_update_variant_prices(shopify_product_id, updates[offset:offset + 100])
        if not result["success"]:
            raise RuntimeError(f"SKU link update failed: {result['errors']}")
    if len(matches) != len(variants):
        raise RuntimeError("Some variants have no unambiguous AliExpress SKU match")
    print(f"[Shopify] Matched SKU IDs for {len(matches)} variants")

def increase_shopify_product_price(shopify_product_id: str, increase_by: float) -> bool:
    """Increase all variants of a Shopify product by a fixed amount — via per-variant PUT so image/inventory links survive."""
    if not settings.SHOPIFY_STORE:
        return False
    try:
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

        success_count = 0
        for variant in variants:
            try:
                current_price = float(variant["price"])
            except (ValueError, TypeError):
                current_price = 0.0
            new_price = current_price + increase_by

            try:
                r2 = requests.put(
                    f"{_base()}/variants/{variant['id']}.json",
                    json={"variant": {"id": variant["id"], "price": f"{new_price:.2f}"}},
                    headers=_h(),
                    timeout=20,
                )
                r2.raise_for_status()
                success_count += 1
            except Exception as e:
                print(f"[Shopify] Price increase failed for variant {variant['id']}: {e}")

        print(f"[Shopify] Increased {success_count}/{len(variants)} variant(s) by {increase_by} for product {shopify_product_id}")
        return success_count > 0
    except Exception as e:
        print(f"[Shopify] Price increase failed: {e}")
        return False




# def update_shopify_product_inventory_with_skus(shopify_product_id: str, aliexpress_skus: list) -> bool:
#     """
#     Push AliExpress per-SKU stock to matching Shopify variants' inventory_quantity.

#     IMPORTANT: Shopify's variant.inventory_quantity is the SUM across all
#     locations. To avoid double-counting when a store has multiple locations,
#     we set the full target quantity at the PRIMARY location and zero out
#     every other location for that inventory item.

#     Variants that have been manually locked (client customized name/price/image)
#     are skipped entirely — auto-sync never touches their stock.
#     """
#     if not settings.SHOPIFY_STORE:
#         return False

#     stock_by_ae_sku = {}
#     for sku in aliexpress_skus:
#         ae_sku_id = str(sku.get("sku_id"))
#         stock = sku.get("stock")
#         if ae_sku_id and ae_sku_id != "None" and stock is not None:
#             try:
#                 stock_by_ae_sku[ae_sku_id] = int(stock)
#             except (ValueError, TypeError):
#                 continue

#     if not stock_by_ae_sku and not aliexpress_skus:
#         print("[Inventory] No AE stock data to sync")
#         return False

#     try:
#         res = requests.get(
#             f"{_base()}/products/{shopify_product_id}.json",
#             params={"fields": "id,variants"},
#             headers=_h(), timeout=15,
#         )
#         res.raise_for_status()
#         shopify_variants = res.json().get("product", {}).get("variants", [])
#         if not shopify_variants:
#             return False
#     except Exception as e:
#         print(f"[Inventory] Fetch error: {e}")
#         return False

#     # ── Skip variants the client has manually locked ──
#     locked_variant_ids = get_locked_variant_ids(shopify_product_id, "inventory")
#     if locked_variant_ids:
#         print(f"[Inventory] {len(locked_variant_ids)} variant(s) locked — will be skipped: {locked_variant_ids}")

#     # ── Get ALL locations once (cache for this call) ──
#     try:
#         loc_res = requests.get(f"{_base()}/locations.json", headers=_h(), timeout=15)
#         loc_res.raise_for_status()
#         locations = loc_res.json().get("locations", [])
#     except Exception as e:
#         print(f"[Inventory] Failed to fetch locations: {e}")
#         return False

#     if not locations:
#         print("[Inventory] No Shopify locations found")
#         return False

#     primary_location_id = locations[0]["id"]
#     other_location_ids = [loc["id"] for loc in locations[1:]]

#     if len(locations) > 1:
#         print(f"[Inventory] Multi-location store detected ({len(locations)} locations). "
#               f"Primary={primary_location_id}, zeroing others={other_location_ids}")

#     variant_ae_map = {}
#     for variant in shopify_variants:
#         vid = variant["id"]
#         try:
#             mf_res = _shopify_request(
#                 "GET", f"{_base()}/variants/{vid}/metafields.json",
#                 params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(),
#             )
#             if mf_res.status_code == 200:
#                 mfs = mf_res.json().get("metafields", [])
#                 if mfs:
#                     variant_ae_map[vid] = mfs[0].get("value")
#         except Exception as e:
#             print(f"[UPDATE] Metafield error for variant {vid}: {e}")  # (or [Inventory] in the other function)

#     matched_via_metafield = any(v["id"] in variant_ae_map for v in shopify_variants)

#     stock_by_label = {}
#     for sku in aliexpress_skus:
#         label = (sku.get("label") or sku.get("sku_attr") or "").strip().lower()
#         stock = sku.get("stock")
#         if label and stock is not None:
#             try:
#                 stock_by_label[label] = int(stock)
#             except (ValueError, TypeError):
#                 continue

#     def _variant_label(variant: dict) -> str:
#         parts = [
#             variant.get(f"option{i}")
#             for i in (1, 2, 3)
#             if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
#         ]
#         return " / ".join(parts).strip().lower()

#     changes = False
#     skipped_locked = 0

#     for i, variant in enumerate(shopify_variants):
#         if variant["id"] in locked_variant_ids:
#             skipped_locked += 1
#             continue  # manually customized — never touch inventory here

#         new_stock = None
#         ae_sku_id = variant_ae_map.get(variant["id"])

#         # 1. Metafield match (most reliable)
#         if ae_sku_id and ae_sku_id in stock_by_ae_sku:
#             new_stock = stock_by_ae_sku[ae_sku_id]

#         # 2. Label fuzzy match
#         elif not matched_via_metafield:
#             label = _variant_label(variant)
#             if label and label in stock_by_label:
#                 new_stock = stock_by_label[label]
#             # 3. Positional fallback — same index in aliexpress_skus
#             elif i < len(aliexpress_skus):
#                 ae_sku = aliexpress_skus[i]
#                 stock_val = ae_sku.get("stock")
#                 if stock_val is not None:
#                     try:
#                         new_stock = int(stock_val)
#                     except (ValueError, TypeError):
#                         new_stock = None

#         if new_stock is None:
#             continue

#         current_stock = variant.get("inventory_quantity")  # SUM across all locations
#         inventory_item_id = variant.get("inventory_item_id")

#         if not inventory_item_id:
#             continue

#         if current_stock == new_stock:
#             if len(locations) <= 1:
#                 continue

#         try:
#             # 1. Set the FULL target quantity at the primary location
#             set_res = requests.post(
#                 f"{_base()}/inventory_levels/set.json",
#                 json={
#                     "location_id": primary_location_id,
#                     "inventory_item_id": inventory_item_id,
#                     "available": new_stock,
#                 },
#                 headers=_h(), timeout=20,
#             )
#             set_res.raise_for_status()

#             # 2. Zero out every OTHER location so the total isn't doubled
#             for other_loc_id in other_location_ids:
#                 try:
#                     zero_res = requests.post(
#                         f"{_base()}/inventory_levels/set.json",
#                         json={
#                             "location_id": other_loc_id,
#                             "inventory_item_id": inventory_item_id,
#                             "available": 0,
#                         },
#                         headers=_h(), timeout=20,
#                     )
#                     # 422 usually means this location isn't connected to this item — safe to ignore
#                     if zero_res.status_code not in (200, 422):
#                         print(f"[Inventory] Could not zero location {other_loc_id} "
#                               f"for variant {variant['id']}: {zero_res.text}")
#                 except Exception as e:
#                     print(f"[Inventory] Error zeroing location {other_loc_id}: {e}")

#             changes = True
#             print(f"[Inventory] Variant {variant['id']}: total {current_stock} → {new_stock} "
#                   f"(set {new_stock} @ location {primary_location_id}"
#                   f"{', zeroed others' if other_location_ids else ''})")
#         except Exception as e:
#             print(f"[Inventory] Update failed for variant {variant['id']}: {e}")

#     if skipped_locked:
#         print(f"[Inventory] Skipped {skipped_locked} locked variant(s) for product {shopify_product_id}")

#     return changes



# def update_shopify_product_inventory_with_skus(shopify_product_id: str, aliexpress_skus: list) -> bool:
#     """
#     Push AliExpress per-SKU stock to matching Shopify variants' inventory_quantity,
#     using ONE GraphQL mutation per product instead of N sequential REST calls.
#     """
#     if not settings.SHOPIFY_STORE:
#         return False

#     stock_by_ae_sku = {}
#     for sku in aliexpress_skus:
#         ae_sku_id = str(sku.get("sku_id"))
#         stock = sku.get("stock")
#         if ae_sku_id and ae_sku_id != "None" and stock is not None:
#             try:
#                 stock_by_ae_sku[ae_sku_id] = int(stock)
#             except (ValueError, TypeError):
#                 continue

#     if not stock_by_ae_sku and not aliexpress_skus:
#         print("[Inventory] No AE stock data to sync")
#         return False

#     try:
#         res = _shopify_request(
#             "GET", f"{_base()}/products/{shopify_product_id}.json",
#             params={"fields": "id,variants"}, headers=_h(), timeout=15,
#         )
#         res.raise_for_status()
#         shopify_variants = res.json().get("product", {}).get("variants", [])
#         if not shopify_variants:
#             return False
#     except Exception as e:
#         print(f"[Inventory] Fetch error: {e}")
#         return False

#     locked_variant_ids = get_locked_variant_ids(shopify_product_id, "inventory")
#     if locked_variant_ids:
#         print(f"[Inventory] {len(locked_variant_ids)} variant(s) locked — will be skipped: {locked_variant_ids}")

#     # ── Locations — filter to ACTIVE only (fixes "location could not be found" errors) ──
#     try:
#         loc_res = _shopify_request("GET", f"{_base()}/locations.json", headers=_h(), timeout=15)
#         loc_res.raise_for_status()
#         all_locations = loc_res.json().get("locations", [])
#     except Exception as e:
#         print(f"[Inventory] Failed to fetch locations: {e}")
#         return False

#     locations = [loc for loc in all_locations if loc.get("active", True)]
#     if not locations:
#         print("[Inventory] No active Shopify locations found")
#         return False
#     if len(locations) < len(all_locations):
#         skipped_ids = [loc["id"] for loc in all_locations if not loc.get("active", True)]
#         print(f"[Inventory] Skipping {len(all_locations) - len(locations)} inactive location(s): {skipped_ids}")

#     primary_location_id = locations[0]["id"]
#     other_location_ids = [loc["id"] for loc in locations[1:]]

#     if len(locations) > 1:
#         print(f"[Inventory] Multi-location store ({len(locations)} active). "
#               f"Primary={primary_location_id}, zeroing others={other_location_ids}")

#     variant_ae_map = {}
#     for variant in shopify_variants:
#         vid = variant["id"]
#         try:
#             mf_res = _shopify_request(
#                 "GET", f"{_base()}/variants/{vid}/metafields.json",
#                 params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(),
#             )
#             if mf_res.status_code == 200:
#                 mfs = mf_res.json().get("metafields", [])
#                 if mfs:
#                     variant_ae_map[vid] = mfs[0].get("value")
#         except Exception as e:
#             print(f"[Inventory] Metafield error for variant {vid}: {e}")

#     stock_by_label = {}
#     for sku in aliexpress_skus:
#         label = (sku.get("label") or sku.get("sku_attr") or "").strip().lower()
#         stock = sku.get("stock")
#         if label and stock is not None:
#             try:
#                 stock_by_label[label] = int(stock)
#             except (ValueError, TypeError):
#                 continue

#     def _variant_label(variant: dict) -> str:
#         parts = [
#             variant.get(f"option{i}")
#             for i in (1, 2, 3)
#             if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
#         ]
#         return " / ".join(parts).strip().lower()

#     # ── Determine target stock per variant — per-variant fallback runs regardless of metafield matches elsewhere ──
#     skipped_locked = 0
#     variant_targets = {}  # {inventory_item_id: new_stock}

#     for i, variant in enumerate(shopify_variants):
#         if variant["id"] in locked_variant_ids:
#             skipped_locked += 1
#             continue

#         new_stock = None
#         ae_sku_id = variant_ae_map.get(variant["id"])

#         if ae_sku_id and ae_sku_id in stock_by_ae_sku:
#             new_stock = stock_by_ae_sku[ae_sku_id]
#         else:
#             label = _variant_label(variant)
#             if label and label in stock_by_label:
#                 new_stock = stock_by_label[label]
#             elif i < len(aliexpress_skus):
#                 stock_val = aliexpress_skus[i].get("stock")
#                 if stock_val is not None:
#                     try:
#                         new_stock = int(stock_val)
#                     except (ValueError, TypeError):
#                         new_stock = None

#         if new_stock is None:
#             continue

#         inventory_item_id = variant.get("inventory_item_id")
#         current_stock = variant.get("inventory_quantity")
#         if not inventory_item_id:
#             continue
#         if current_stock == new_stock and len(locations) <= 1:
#             continue  # already correct on a single-location store — nothing to do

#         variant_targets[inventory_item_id] = new_stock

#     if skipped_locked:
#         print(f"[Inventory] Skipped {skipped_locked} locked variant(s) for product {shopify_product_id}")

#     if not variant_targets:
#         print("[Inventory] No inventory changes needed")
#         return False

#     # ── ONE bulk GraphQL mutation: primary = target, all other active locations = 0 ──
#     bulk_quantities = []
#     for inventory_item_id, new_stock in variant_targets.items():
#         bulk_quantities.append({
#             "inventory_item_id": inventory_item_id,
#             "location_id": primary_location_id,
#             "quantity": new_stock,
#         })
#         for other_loc_id in other_location_ids:
#             bulk_quantities.append({
#                 "inventory_item_id": inventory_item_id,
#                 "location_id": other_loc_id,
#                 "quantity": 0,
#             })

#     result = bulk_set_inventory_quantities(bulk_quantities)
#     if result["success"]:
#         print(f"[Inventory] Bulk-updated {len(variant_targets)} variant(s) for product {shopify_product_id} "
#               f"({len(bulk_quantities)} item/location pairs in 1 API call)")
#         return True
#     else:
#         print(f"[Inventory] Bulk update failed: {result['errors']}")
#         return False






def update_shopify_product_inventory_with_skus(shopify_product_id: str, aliexpress_skus: list) -> bool:
    """
    Push AliExpress per-SKU stock to matching Shopify variants' inventory_quantity.

    IMPORTANT: Shopify's variant.inventory_quantity is the SUM across all
    locations. To avoid double-counting when a store has multiple locations,
    we set the full target quantity at the PRIMARY location and zero out
    every other location for that inventory item.

    Variants that have been manually locked (client customized name/price/image)
    are skipped entirely — auto-sync never touches their stock.
    """
    if not settings.SHOPIFY_STORE:
        return False

    if not aliexpress_skus:
        print("[Inventory] No AE stock data to sync")
        return False

    try:
        res = requests.get(
            f"{_base()}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"},
            headers=_h(), timeout=15,
        )
        res.raise_for_status()
        shopify_variants = res.json().get("product", {}).get("variants", [])
        if not shopify_variants:
            return False
    except Exception as e:
        print(f"[Inventory] Fetch error: {e}")
        return False

    # ── Skip variants the client has manually locked ──
    locked_variant_ids = get_locked_variant_ids(shopify_product_id, "inventory")
    if locked_variant_ids:
        print(f"[Inventory] {len(locked_variant_ids)} variant(s) locked — will be skipped: {locked_variant_ids}")

    # ── Get ALL locations once (cache for this call) ──
    try:
        loc_res = requests.get(f"{_base()}/locations.json", headers=_h(), timeout=15)
        loc_res.raise_for_status()
        locations = loc_res.json().get("locations", [])
    except Exception as e:
        print(f"[Inventory] Failed to fetch locations: {e}")
        return False

    if not locations:
        print("[Inventory] No Shopify locations found")
        return False

    primary_location_id = locations[0]["id"]
    other_location_ids = [loc["id"] for loc in locations[1:]]

    if len(locations) > 1:
        print(f"[Inventory] Multi-location store detected ({len(locations)} locations). "
              f"Primary={primary_location_id}, zeroing others={other_location_ids}")

    variant_ae_map = {}
    for variant in shopify_variants:
        vid = variant["id"]
        try:
            mf_res = _shopify_request(
                "GET", f"{_base()}/variants/{vid}/metafields.json",
                params={"namespace": "aliexpress", "key": "sku_id"}, headers=_h(),
            )
            if mf_res.status_code == 200:
                mfs = mf_res.json().get("metafields", [])
                if mfs:
                    variant_ae_map[vid] = mfs[0].get("value")
        except Exception as e:
            print(f"[Inventory] Metafield error for variant {vid}: {e}")

    def _variant_label(variant: dict) -> str:
        parts = [
            variant.get(f"option{i}")
            for i in (1, 2, 3)
            if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
        ]
        return " / ".join(parts).strip().lower()

    changes = False
    skipped_locked = 0
    unmatched_variants = []

    matches = _match_supplier_variants({v["id"]: {
        "label": _variant_label(v), "ae_sku_id": variant_ae_map.get(v["id"]),
    } for v in shopify_variants}, aliexpress_skus)

    for variant in shopify_variants:
        if variant["id"] in locked_variant_ids:
            skipped_locked += 1
            continue  # manually customized — never touch inventory here

        new_stock = None
        sku = matches.get(variant["id"])
        if sku is not None and sku.get("stock") is not None:
            try:
                new_stock = int(sku["stock"])
            except (ValueError, TypeError):
                pass

        if new_stock is None:
            unmatched_variants.append(variant["id"])
            continue

        current_stock = variant.get("inventory_quantity")  # SUM across all locations
        inventory_item_id = variant.get("inventory_item_id")

        if not inventory_item_id:
            continue

        if current_stock == new_stock:
            if len(locations) <= 1:
                continue

        try:
            # 1. Set the FULL target quantity at the primary location
            set_res = requests.post(
                f"{_base()}/inventory_levels/set.json",
                json={
                    "location_id": primary_location_id,
                    "inventory_item_id": inventory_item_id,
                    "available": new_stock,
                },
                headers=_h(), timeout=20,
            )
            set_res.raise_for_status()

            # 2. Zero out every OTHER location so the total isn't doubled
            for other_loc_id in other_location_ids:
                try:
                    zero_res = requests.post(
                        f"{_base()}/inventory_levels/set.json",
                        json={
                            "location_id": other_loc_id,
                            "inventory_item_id": inventory_item_id,
                            "available": 0,
                        },
                        headers=_h(), timeout=20,
                    )
                    if zero_res.status_code not in (200, 422):
                        print(f"[Inventory] Could not zero location {other_loc_id} "
                              f"for variant {variant['id']}: {zero_res.text}")
                except Exception as e:
                    print(f"[Inventory] Error zeroing location {other_loc_id}: {e}")

            changes = True
            print(f"[Inventory] Variant {variant['id']}: total {current_stock} → {new_stock} "
                  f"(set {new_stock} @ location {primary_location_id}"
                  f"{', zeroed others' if other_location_ids else ''})")
        except Exception as e:
            print(f"[Inventory] Update failed for variant {variant['id']}: {e}")

    if skipped_locked:
        print(f"[Inventory] Skipped {skipped_locked} locked variant(s) for product {shopify_product_id}")
    if unmatched_variants:
        print(f"[Inventory] {len(unmatched_variants)} variant(s) had NO stock match at all: {unmatched_variants}")

    return changes





def set_product_out_of_stock(shopify_product_id: str) -> bool:
    """
    Zero out inventory for ALL variants of a Shopify product across ALL locations,
    AND set inventory_policy to "deny" so Shopify actually shows/enforces
    "Out of stock" instead of silently allowing continued sales at 0 qty.

    Used when an AliExpress listing is confirmed dead (no prices) and hasn't been
    remapped yet — we don't want to keep selling something we can no longer source.

    Skips variants that are inventory-locked (manually protected from auto-sync).
    Does NOT touch price or images.

    Uses _shopify_request() for rate-limit safety during bulk scans.
    """
    if not settings.SHOPIFY_STORE:
        return False

    try:
        res = _shopify_request(
            "GET", f"{_base()}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"}, headers=_h(), timeout=15,
        )
        res.raise_for_status()
        variants = res.json().get("product", {}).get("variants", [])
        if not variants:
            print(f"[DeadStock] No variants found for {shopify_product_id}")
            return False
    except Exception as e:
        print(f"[DeadStock] Fetch variants failed: {e}")
        return False

    try:
        loc_res = _shopify_request("GET", f"{_base()}/locations.json", headers=_h(), timeout=15)
        loc_res.raise_for_status()
        locations = loc_res.json().get("locations", [])
    except Exception as e:
        print(f"[DeadStock] Failed to fetch locations: {e}")
        return False

    if not locations:
        print(f"[DeadStock] No Shopify locations found")
        return False

    # Skip variants the client has manually locked against inventory changes
    locked_variant_ids = get_locked_variant_ids(shopify_product_id, "inventory")
    if locked_variant_ids:
        print(f"[DeadStock] {len(locked_variant_ids)} variant(s) inventory-locked — will be skipped: {locked_variant_ids}")

    success_count = 0
    skipped_locked = 0
    policy_failures = 0

    for variant in variants:
        if variant["id"] in locked_variant_ids:
            skipped_locked += 1
            continue

        inventory_item_id = variant.get("inventory_item_id")
        if not inventory_item_id:
            continue

        variant_ok = True

        # 1. Zero out stock at every location (throttled + auto-retry on 429)
        for loc in locations:
            try:
                res2 = _shopify_request(
                    "POST", f"{_base()}/inventory_levels/set.json",
                    json={
                        "location_id": loc["id"],
                        "inventory_item_id": inventory_item_id,
                        "available": 0,
                    },
                    headers=_h(), timeout=20,
                )
                # 422 usually means this location isn't connected to this item — safe to ignore
                if res2.status_code not in (200, 422):
                    variant_ok = False
                    print(f"[DeadStock] Failed to zero variant {variant['id']} @ location {loc['id']}: {res2.text}")
            except Exception as e:
                variant_ok = False
                print(f"[DeadStock] Error zeroing variant {variant['id']} @ location {loc['id']}: {e}")

        # 2. Force inventory_policy to "deny" so 0 stock actually blocks purchase
        #    and the storefront shows "Out of stock" instead of staying buyable.
        try:
            policy_res = _shopify_request(
                "PUT", f"{_base()}/variants/{variant['id']}.json",
                json={"variant": {"id": variant["id"], "inventory_policy": "deny"}},
                headers=_h(), timeout=20,
            )
            if policy_res.status_code != 200:
                variant_ok = False
                policy_failures += 1
                print(f"[DeadStock] Failed to set inventory_policy=deny for variant {variant['id']}: {policy_res.text}")
        except Exception as e:
            variant_ok = False
            policy_failures += 1
            print(f"[DeadStock] Error setting inventory_policy for variant {variant['id']}: {e}")

        if variant_ok:
            success_count += 1

    if skipped_locked:
        print(f"[DeadStock] Skipped {skipped_locked} inventory-locked variant(s) for product {shopify_product_id}")
    if policy_failures:
        print(f"[DeadStock] {policy_failures} variant(s) had inventory_policy update failures")

    print(f"[DeadStock] Zeroed inventory + set deny-policy for {success_count}/{len(variants)} variant(s) of product {shopify_product_id}")
    return success_count > 0



# ─────────────────────────────────────────────
# VARIANT LOCK (manual override protection)
# ─────────────────────────────────────────────

# LOCK_TYPES = ("price", "inventory", "image")

# def get_variant_lock_map(shopify_product_id: str) -> dict:
#     """Fetch every variant's lock flags in one pass.
#     Returns {variant_id: {"price": bool, "inventory": bool, "image": bool}}"""
#     lock_map = {}
#     try:
#         res = _shopify_request(
#             "GET", f"{_base()}/products/{shopify_product_id}.json",
#             params={"fields": "id,variants"}, headers=_h(),
#         )
#         res.raise_for_status()
#         variants = res.json().get("product", {}).get("variants", [])
#     except Exception as e:
#         print(f"[Lock] Failed to fetch variants for {shopify_product_id}: {e}")
#         return lock_map

#     for v in variants:
#         vid = v["id"]
#         flags = {"price": False, "inventory": False, "image": False}
#         try:
#             mf_res = _shopify_request(
#                 "GET", f"{_base()}/variants/{vid}/metafields.json",
#                 params={"namespace": "sync"}, headers=_h(),
#             )
#             if mf_res.status_code == 200:
#                 for mf in mf_res.json().get("metafields", []):
#                     key = mf.get("key", "")
#                     if key.endswith("_locked") and mf.get("value") == "true":
#                         lt = key.replace("_locked", "")
#                         if lt in flags:
#                             flags[lt] = True
#             else:
#                 print(f"[Lock] Metafield fetch failed for variant {vid}: HTTP {mf_res.status_code}")
#         except Exception as e:
#             print(f"[Lock] Metafield fetch failed for variant {vid}: {e}")
#         lock_map[vid] = flags
#     return lock_map
 

_lock_cache: dict[str, tuple[float, dict]] = {}
_LOCK_CACHE_TTL = 60  # seconds

LOCK_TYPES = ("price", "inventory", "image")

# def get_variant_lock_map(shopify_product_id: str, use_cache: bool = True) -> dict:
#     """Fetch every variant's lock flags in one pass.
#     Returns {variant_id: {"price": bool, "inventory": bool, "image": bool}}
#     Cached for _LOCK_CACHE_TTL seconds to avoid N sequential Shopify calls
#     on every modal open — lock status changes infrequently."""
#     if use_cache:
#         cached = _lock_cache.get(shopify_product_id)
#         if cached and (_time.time() - cached[0]) < _LOCK_CACHE_TTL:
#             return cached[1]

#     lock_map = {}
#     try:
#         res = _shopify_request(
#             "GET", f"{_base()}/products/{shopify_product_id}.json",
#             params={"fields": "id,variants"}, headers=_h(),
#         )
#         res.raise_for_status()
#         variants = res.json().get("product", {}).get("variants", [])
#     except Exception as e:
#         print(f"[Lock] Failed to fetch variants for {shopify_product_id}: {e}")
#         return lock_map

#     for v in variants:
#         vid = v["id"]
#         flags = {"price": False, "inventory": False, "image": False}
#         try:
#             mf_res = _shopify_request(
#                 "GET", f"{_base()}/variants/{vid}/metafields.json",
#                 params={"namespace": "sync"}, headers=_h(),
#             )
#             if mf_res.status_code == 200:
#                 for mf in mf_res.json().get("metafields", []):
#                     key = mf.get("key", "")
#                     if key.endswith("_locked") and mf.get("value") == "true":
#                         lt = key.replace("_locked", "")
#                         if lt in flags:
#                             flags[lt] = True
#             else:
#                 print(f"[Lock] Metafield fetch failed for variant {vid}: HTTP {mf_res.status_code}")
#         except Exception as e:
#             print(f"[Lock] Metafield fetch failed for variant {vid}: {e}")
#         lock_map[vid] = flags

#     _lock_cache[shopify_product_id] = (_time.time(), lock_map)
#     return lock_map

def get_variant_sync_state(shopify_product_id: str) -> dict:
    """Read all prices, SKU links and rules together; never default failed reads."""
    query = """
    query($id: ID!, $after: String) {
      product(id: $id) {
        variants(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          edges { node {
            id price selectedOptions { name value }
            aeSku: metafield(namespace: "aliexpress", key: "sku_id") { value }
            increase: metafield(namespace: "sync", key: "price_increase") { id value }
            supplierHistory: metafield(namespace: "sync", key: "supplier_price_history") { id value }
            priceLock: metafield(namespace: "sync", key: "price_locked") { value }
            inventoryLock: metafield(namespace: "sync", key: "inventory_locked") { value }
            imageLock: metafield(namespace: "sync", key: "image_locked") { value }
          } }
        }
      }
    }
    """
    import json
    from decimal import Decimal
    state, cursor = {}, None
    while True:
        data = _graphql(query, {"id": _product_gid(shopify_product_id), "after": cursor})
        if not data.get("product"):
            raise RuntimeError("Shopify variant pricing rules could not be read")
        connection = data["product"]["variants"]
        for edge in connection["edges"]:
            node = edge["node"]
            vid = int(_numeric_id_from_gid(node["id"]))
            increase = Decimal((node.get("increase") or {}).get("value", "0"))
            if not increase.is_finite():
                raise ValueError(f"Invalid saved increase for variant {vid}")
            state[vid] = {
                "id": vid, "price": node["price"],
                "ae_sku_id": (node.get("aeSku") or {}).get("value"),
                "price_increase": increase,
                "supplier_history": json.loads(node["supplierHistory"]["value"]) if node.get("supplierHistory") else None,
                "supplier_history_id": (node.get("supplierHistory") or {}).get("id"),
                "increase_metafield_id": (node.get("increase") or {}).get("id"),
                "label": " / ".join(option["value"] for option in node["selectedOptions"]
                                     if option["value"] != "Default Title"),
                "locks": {field: (node.get(field + "Lock") or {}).get("value") == "true"
                          for field in LOCK_TYPES},
            }
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return state
        next_cursor = page["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("Shopify variant pagination did not advance")
        cursor = next_cursor


def get_variant_lock_map(shopify_product_id: str, use_cache: bool = True) -> dict:
    if use_cache:
        cached = _lock_cache.get(shopify_product_id)
        if cached and (_time.time() - cached[0]) < _LOCK_CACHE_TTL:
            return cached[1]
    lock_map = {vid: row["locks"] for vid, row in get_variant_sync_state(shopify_product_id).items()}
    _lock_cache[shopify_product_id] = (_time.time(), lock_map)
    return lock_map


def _invalidate_lock_cache(shopify_product_id: str):
    _lock_cache.pop(shopify_product_id, None)





def get_locked_variant_ids(shopify_product_id: str, lock_type: str) -> set:
    return {vid for vid, flags in get_variant_lock_map(shopify_product_id).items() if flags.get(lock_type)}


def get_variant_price_increase_map(shopify_product_id: str) -> dict[int, float]:
    return {vid: float(row["price_increase"])
            for vid, row in get_variant_sync_state(shopify_product_id).items()
            if row["price_increase"]}


def save_variant_price_edits(shopify_product_id: str, edits: list) -> list:
    """Save each final price and its persistent adjustment in the same mutation."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    state = get_variant_sync_state(shopify_product_id)
    updates, seen = [], set()
    for edit in edits:
        try:
            vid = int(edit["variant_id"])
            price = Decimal(str(edit["price"]))
            if vid not in state or vid in seen:
                raise ValueError("Variant is missing from this product or duplicated")
            if not price.is_finite() or price < 0:
                raise ValueError("Price must be finite and non-negative")
            price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            previous = state[vid]
            # Older clients may submit an absolute price without an increase.
            # An absolute edit to a locked selling price preserves its markup.
            # Only an explicit increase changes that saved rule while locked.
            # For unlocked variants, derive the adjustment from the current price.
            increase = (Decimal(str(edit["price_increase"])) if "price_increase" in edit
                        else previous["price_increase"] if previous["locks"]["price"]
                        else previous["price_increase"] + price - Decimal(str(previous["price"])))
            if not increase.is_finite():
                raise ValueError("Increase must be finite")
            increase = increase.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            updates.append({"variant_id": vid, "price": str(price), "price_increase": str(increase),
                            "increase_metafield_id": previous.get("increase_metafield_id")})
            seen.add(vid)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise HTTPException(400, f"Invalid variant price edit: {exc}") from exc
    if not updates:
        raise HTTPException(400, "No variant price edits provided")
    # Validate the complete request before writing any batches.
    for offset in range(0, len(updates), 100):
        result = bulk_update_variant_prices(shopify_product_id, updates[offset:offset + 100])
        if not result["success"]:
            raise HTTPException(502, f"Failed to save variant prices and increases: {result['errors']}")
    _invalidate_lock_cache(shopify_product_id)
    return updates


def set_variant_price_increase(variant_id: int, amount: float) -> bool:
    """Persist a per-variant increase used by future AliExpress price syncs."""
    try:
        res = _shopify_request(
            "GET", f"{_base()}/variants/{variant_id}/metafields.json",
            params={"namespace": "sync", "key": "price_increase"}, headers=_h(),
        )
        existing = res.json().get("metafields", []) if res.status_code == 200 else []
        payload = {"metafield": {
            "namespace": "sync", "key": "price_increase",
            "value": str(float(amount)), "type": "number_decimal",
        }}
        if existing:
            saved = _shopify_request("PUT", f"{_base()}/metafields/{existing[0]['id']}.json", json=payload, headers=_h())
        else:
            saved = _shopify_request("POST", f"{_base()}/variants/{variant_id}/metafields.json", json=payload, headers=_h())
        saved.raise_for_status()
        return True
    except Exception as e:
        print(f"[VariantIncrease] Save failed for variant {variant_id}: {e}")
        return False


def is_variant_locked(variant_id: int, lock_type: str) -> bool:
    try:
        res = _shopify_request(
            "GET", f"{_base()}/variants/{variant_id}/metafields.json",
            params={"namespace": "sync", "key": f"{lock_type}_locked"}, headers=_h(),
        )
        if res.status_code != 200:
            return False
        mfs = res.json().get("metafields", [])
        return bool(mfs) and mfs[0].get("value") == "true"
    except Exception as e:
        print(f"[Lock] Check failed for variant {variant_id} ({lock_type}): {e}")
        return False


def set_variant_lock(variant_id: int, lock_type: str, locked: bool) -> bool:
    if lock_type not in LOCK_TYPES:
        return False
    key = f"{lock_type}_locked"
    try:
        res = _shopify_request(
            "GET", f"{_base()}/variants/{variant_id}/metafields.json",
            params={"namespace": "sync", "key": key}, headers=_h(),
        )
        existing = res.json().get("metafields", []) if res.status_code == 200 else []
        payload = {"metafield": {"namespace": "sync", "key": key, "value": "true" if locked else "false", "type": "single_line_text_field"}}
        if existing:
            r2 = _shopify_request("PUT", f"{_base()}/metafields/{existing[0]['id']}.json", json=payload, headers=_h())
        else:
            r2 = _shopify_request("POST", f"{_base()}/variants/{variant_id}/metafields.json", json=payload, headers=_h())
        r2.raise_for_status()
        print(f"[Lock] Variant {variant_id} {lock_type} {'locked' if locked else 'unlocked'}")
        return True
    except Exception as e:
        print(f"[Lock] Failed to set {lock_type} lock for variant {variant_id}: {e}")
        return False

def _throttle():
    global _last_call_time
    with _rate_lock:
        now = time.time()
        wait = MIN_INTERVAL - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.time()


def _shopify_request(method: str, url: str, max_retries: int = 5, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 20)
    for attempt in range(max_retries):
        _throttle()
        try:
            res = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                raise
            delay = min(float(attempt + 1), 5.0)
            print(f"[Shopify][Retry] {method} request failed: {exc}; retrying in {delay:.1f}s")
            time.sleep(delay)
            continue
        if res.status_code != 429 and res.status_code not in (500, 502, 503, 504):
            return res
        retry_after = res.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 1.0
        except (ValueError, TypeError):
            delay = 1.0
        delay = min(max(delay, 0.5) * (attempt + 1), 5.0)
        print(f"[Shopify][Retry] HTTP {res.status_code} on {method} {url} — retrying in {delay:.1f}s (attempt {attempt+1}/{max_retries})")
        time.sleep(delay)
    print(f"[Shopify][RateLimit] Giving up after {max_retries} retries: {method} {url}")
    return res

def _fetch_inventory_levels_bulk(inventory_item_ids: list) -> dict:
    """
    Returns {inventory_item_id: {location_id: available}} using Shopify's
    batch inventory_levels endpoint — ONE call covers many items across
    ALL their locations, instead of one call per item per location.
    Shopify allows up to 50 inventory_item_ids per request.
    """
    if not inventory_item_ids:
        return {}
    levels_map = {}
    CHUNK = 50
    for i in range(0, len(inventory_item_ids), CHUNK):
        chunk = inventory_item_ids[i:i + CHUNK]
        ids_param = ",".join(str(x) for x in chunk)
        try:
            res = _shopify_request(
                "GET", f"{_base()}/inventory_levels.json",
                params={"inventory_item_ids": ids_param, "limit": 250},
                headers=_h(), timeout=20,
            )
            if res.status_code == 200:
                for lvl in res.json().get("inventory_levels", []):
                    iid = lvl["inventory_item_id"]
                    levels_map.setdefault(iid, {})[lvl["location_id"]] = lvl.get("available", 0)
            else:
                print(f"[Inventory] Bulk levels fetch failed: HTTP {res.status_code}")
        except Exception as e:
            print(f"[Inventory] Bulk levels fetch error: {e}")
    return levels_map

def _graphql_url() -> str:
    shop = settings.SHOPIFY_STORE.replace(".myshopify.com", "").strip()
    return f"https://{shop}.myshopify.com/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"


def _graphql(query: str, variables: dict = None) -> dict:
    """Single throttled/retried POST to Shopify's GraphQL Admin API."""
    payload = {"query": query, "variables": variables or {}}
    res = _shopify_request("POST", _graphql_url(), json=payload, headers=_h(), timeout=30)
    res.raise_for_status()
    data = res.json()
    if "errors" in data:
        raise HTTPException(502, f"Shopify GraphQL error: {data['errors']}")
    return data.get("data", {})


def _variant_gid(variant_id) -> str:
    return f"gid://shopify/ProductVariant/{variant_id}"

def _product_gid(product_id) -> str:
    return f"gid://shopify/Product/{product_id}"

def _inventory_item_gid(item_id) -> str:
    return f"gid://shopify/InventoryItem/{item_id}"

def _location_gid(location_id) -> str:
    return f"gid://shopify/Location/{location_id}"

def _numeric_id_from_gid(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]

def bulk_update_variant_prices(shopify_product_id: str, variant_prices: list) -> dict:
    """
    variant_prices: [{"variant_id": 123, "price": "19.99"}, ...]
    Updates ALL variant prices in ONE GraphQL mutation instead of N sequential REST PUTs.
    Returns {"success": bool, "updated": int, "errors": list}
    """
    if not variant_prices:
        return {"success": True, "updated": 0, "errors": []}

    mutation = """
    mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants, allowPartialUpdates: false) {
        productVariants { id price priceIncrease: metafield(namespace: "sync", key: "price_increase") { value }
          aeSku: metafield(namespace: "aliexpress", key: "sku_id") { value } }
        userErrors { field message }
      }
    }
    """
    variants_input = []
    for edit in variant_prices:
        row = {"id": _variant_gid(edit["variant_id"])}
        if "price" in edit:
            row["price"] = str(edit["price"])
        if "price_increase" in edit:
            metafield = {"value": str(edit["price_increase"])}
            if edit.get("increase_metafield_id"):
                metafield["id"] = edit["increase_metafield_id"]
            else:
                metafield.update({"namespace": "sync", "key": "price_increase", "type": "number_decimal"})
            row["metafields"] = [metafield]
        if "supplier_history" in edit:
            import json
            history = {"value": json.dumps(edit["supplier_history"])}
            if edit.get("supplier_history_id"):
                history["id"] = edit["supplier_history_id"]
            else:
                history.update({"namespace": "sync", "key": "supplier_price_history", "type": "json"})
            row.setdefault("metafields", []).append(history)
        if "ae_sku_id" in edit:
            row.setdefault("metafields", []).append({"namespace": "aliexpress", "key": "sku_id",
                "type": "single_line_text_field", "value": str(edit["ae_sku_id"])})
        variants_input.append(row)
    try:
        data = _graphql(mutation, {
            "productId": _product_gid(shopify_product_id),
            "variants": variants_input,
        })
        result = data.get("productVariantsBulkUpdate", {})
        errors = result.get("userErrors", [])
        returned = {row["id"]: row for row in result.get("productVariants", [])}
        updated = len(returned)
        from decimal import Decimal
        for edit in variant_prices:
            if "ae_sku_id" in edit:
                saved = returned.get(_variant_gid(edit["variant_id"]), {})
                if (saved.get("aeSku") or {}).get("value") != str(edit["ae_sku_id"]):
                    errors.append({"message": f"SKU link was not confirmed for variant {edit['variant_id']}"})
            if "price_increase" in edit:
                saved = returned.get(_variant_gid(edit["variant_id"]), {})
                amount = (saved.get("priceIncrease") or {}).get("value")
                if amount is None or Decimal(amount) != Decimal(str(edit["price_increase"])):
                    errors.append({"message": f"Increase was not confirmed for variant {edit['variant_id']}"})
        if errors:
            print(f"[BulkPrice] userErrors: {errors}")
        return {"success": not errors and updated == len(variants_input), "updated": updated, "errors": errors}
    except Exception as e:
        print(f"[BulkPrice] Mutation failed: {e}")
        return {"success": False, "updated": 0, "errors": [str(e)]}


# def bulk_set_inventory_quantities(quantities: list) -> dict:
#     """
#     quantities: [{"inventory_item_id": 111, "location_id": 222, "quantity": 5}, ...]
#     Sets inventory across MANY item/location pairs in ONE GraphQL mutation,
#     instead of N REST calls per variant per location.
#     Returns {"success": bool, "errors": list}
#     """
#     if not quantities:
#         return {"success": True, "errors": []}

#     mutation = """
#     mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
#       inventorySetQuantities(input: $input) {
#         userErrors { field message }
#       }
#     }
#     """
#     input_quantities = [
#         {
#             "inventoryItemId": _inventory_item_gid(q["inventory_item_id"]),
#             "locationId": _location_gid(q["location_id"]),
#             "quantity": q["quantity"],
#         }
#         for q in quantities
#     ]
#     try:
#         data = _graphql(mutation, {
#             "input": {
#                 "name": "available",
#                 "reason": "correction",
#                 "ignoreCompareQuantity": True,
#                 "quantities": input_quantities,
#             }
#         })
#         errors = data.get("inventorySetQuantities", {}).get("userErrors", [])
#         if errors:
#             print(f"[BulkInventory] userErrors: {errors}")
#         return {"success": not errors, "errors": errors}
#     except Exception as e:
#         print(f"[BulkInventory] Mutation failed: {e}")
#         return {"success": False, "errors": [str(e)]}



def bulk_set_inventory_quantities(quantities: list) -> dict:
    """
    quantities: [{"inventory_item_id": 111, "location_id": 222, "quantity": 5}, ...]
    Sets inventory across many item/location pairs in ONE GraphQL mutation.
    If specific locations are rejected ("could not be found"), retries once
    without those entries instead of failing the whole batch.
    """
    if not quantities:
        return {"success": True, "errors": []}

    result = _run_inventory_mutation(quantities)
    if result["success"]:
        return result

    bad_indices = set()
    for err in result["errors"]:
        field = err.get("field", [])
        if len(field) >= 2 and field[-1] == "locationId":
            try:
                bad_indices.add(int(field[-2]))
            except (ValueError, IndexError):
                pass

    if bad_indices and len(bad_indices) < len(quantities):
        print(f"[BulkInventory] Retrying without {len(bad_indices)} invalid location entries")
        filtered = [q for i, q in enumerate(quantities) if i not in bad_indices]
        return _run_inventory_mutation(filtered)

    return result


def _run_inventory_mutation(quantities: list) -> dict:
    mutation = """
    mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
      inventorySetQuantities(input: $input) {
        userErrors { field message }
      }
    }
    """
    input_quantities = [
        {
            "inventoryItemId": _inventory_item_gid(q["inventory_item_id"]),
            "locationId": _location_gid(q["location_id"]),
            "quantity": q["quantity"],
        }
        for q in quantities
    ]
    try:
        data = _graphql(mutation, {
            "input": {
                "name": "available",
                "reason": "correction",
                "ignoreCompareQuantity": True,
                "quantities": input_quantities,
            }
        })
        errors = data.get("inventorySetQuantities", {}).get("userErrors", [])
        if errors:
            print(f"[BulkInventory] userErrors: {errors}")
        return {"success": not errors, "errors": errors}
    except Exception as e:
        print(f"[BulkInventory] Mutation failed: {e}")
        return {"success": False, "errors": [str(e)]}
