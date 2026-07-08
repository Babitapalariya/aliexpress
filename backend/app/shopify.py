"""
shopify.py – complete with all functions needed by main.py.
Now includes per-variant SKU image upload via Shopify Images API.
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


def attach_sku_images_to_product(shopify_product_id: str, aliexpress_skus: list, shopify_variants: list) -> int:
    """
    For each AliExpress SKU that has an "image" URL, upload it to Shopify
    and link it to the matching variant.

    aliexpress_skus  — list of dicts from get_product() with keys: sku_id, label, image, ...
    shopify_variants — list of Shopify variant dicts (with "id" and "image_id")

    Returns the count of variants that had an image successfully attached.
    """
    if not aliexpress_skus or not shopify_variants:
        return 0

    # Build a URL → Shopify image_id cache to avoid uploading the same image twice
    # (multiple SKUs can share the same colour swatch image)
    url_to_image_id: dict[str, int] = {}

    # Build AliExpress SKU index by position (same order as Shopify variants)
    attached = 0

    for i, shopify_variant in enumerate(shopify_variants):
        if i >= len(aliexpress_skus):
            break

        ae_sku = aliexpress_skus[i]
        img_url = ae_sku.get("image")

        if not img_url:
            continue

        # Upload or reuse
        if img_url not in url_to_image_id:
            image_id = _upload_image_to_shopify(
                shopify_product_id,
                img_url,
                alt=ae_sku.get("label", ""),
            )
            if image_id:
                url_to_image_id[img_url] = image_id
            else:
                continue
        else:
            image_id = url_to_image_id[img_url]

        # Link image to variant
        variant_id = shopify_variant["id"]
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

    return attached


def backfill_sku_images(shopify_product_id: str, aliexpress_skus: list) -> dict:
    """
    Fetch current Shopify variants, then attach any missing SKU images.
    Skips variants that already have an image_id.
    Returns {"attached": int, "skipped": int, "total_variants": int}
    """
    try:
        res = requests.get(
            f"{_base()}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"},
            headers=_h(), timeout=15,
        )
        res.raise_for_status()
        shopify_variants = res.json().get("product", {}).get("variants", [])
    except Exception as e:
        print(f"[Backfill][Image] Fetch variants failed: {e}")
        return {"attached": 0, "skipped": 0, "total_variants": 0}

    # Only process variants that don't have an image yet
    needs_image = [v for v in shopify_variants if not v.get("image_id")]
    already_has = len(shopify_variants) - len(needs_image)

    if not needs_image:
        return {"attached": 0, "skipped": already_has, "total_variants": len(shopify_variants)}

    # Build matching sku list for the ones that need images
    # We map by position — same assumption as store_aliexpress_sku_ids
    url_to_image_id: dict[str, int] = {}
    attached = 0

    for i, shopify_variant in enumerate(shopify_variants):
        if shopify_variant.get("image_id"):
            continue  # already has one
        if i >= len(aliexpress_skus):
            break

        ae_sku = aliexpress_skus[i]
        img_url = ae_sku.get("image")
        if not img_url:
            continue

        if img_url not in url_to_image_id:
            image_id = _upload_image_to_shopify(
                shopify_product_id,
                img_url,
                alt=ae_sku.get("label", ""),
            )
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
            print(f"[Backfill][Image] Variant {variant_id} ← image {image_id}")
        except Exception as e:
            print(f"[Backfill][Image] Link failed {variant_id}: {e}")

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


def update_shopify_product_prices_with_skus(shopify_product_id: str, aliexpress_skus: list) -> str:
    """
    Returns one of: "updated", "unchanged", "failed"
    """
    if not settings.SHOPIFY_STORE:
        return "failed"
    print(f"\n[UPDATE] Starting variant price sync for product {shopify_product_id}")

    price_by_ae_sku = {}
    for sku in aliexpress_skus:
        ae_sku_id = str(sku.get("sku_id"))
        price = sku.get("sale_price") or sku.get("price")
        if ae_sku_id and ae_sku_id != "None" and price:
            price_by_ae_sku[ae_sku_id] = float(price)

    if not price_by_ae_sku and not aliexpress_skus:
        print("[UPDATE] No AE SKU IDs")
        return "failed"

    try:
        res = requests.get(
            f"{_base()}/products/{shopify_product_id}.json",
            params={"fields": "id,variants"},
            headers=_h(), timeout=15,
        )
        res.raise_for_status()
        shopify_variants = res.json().get("product", {}).get("variants", [])
        if not shopify_variants:
            return "failed"
    except Exception as e:
        print(f"[UPDATE] Fetch error: {e}")
        return "failed"

    variant_ae_map = {}
    for variant in shopify_variants:
        vid = variant["id"]
        try:
            mf_res = requests.get(
                f"{_base()}/variants/{vid}/metafields.json",
                params={"namespace": "aliexpress", "key": "sku_id"},
                headers=_h(), timeout=10,
            )
            if mf_res.status_code == 200:
                mfs = mf_res.json().get("metafields", [])
                if mfs:
                    variant_ae_map[vid] = mfs[0].get("value")
        except Exception as e:
            print(f"[UPDATE] Metafield error for variant {vid}: {e}")

    matched_via_metafield = any(v["id"] in variant_ae_map for v in shopify_variants)

    price_by_label = {}
    for sku in aliexpress_skus:
        label = (sku.get("label") or sku.get("sku_attr") or "").strip().lower()
        price = sku.get("sale_price") or sku.get("price")
        if label and price:
            price_by_label[label] = float(price)

    def _variant_label(variant: dict) -> str:
        parts = [
            variant.get(f"option{i}")
            for i in (1, 2, 3)
            if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
        ]
        return " / ".join(parts).strip().lower()

    def _fuzzy_label_match(variant_label: str):
        if not variant_label:
            return None
        if variant_label in price_by_label:
            return price_by_label[variant_label]
        variant_tokens = set(t.strip() for t in variant_label.split("/"))
        for label, price in price_by_label.items():
            label_tokens = set(t.strip() for t in label.split("/"))
            if variant_tokens and variant_tokens.issubset(label_tokens):
                return price
        return None

    print(f"[UPDATE][DEBUG] price_by_ae_sku = {price_by_ae_sku}")
    print(f"[UPDATE][DEBUG] price_by_label = {price_by_label}")
    print(f"[UPDATE][DEBUG] matched_via_metafield = {matched_via_metafield}")

    # Build a list of (variant_id, new_price) for variants that actually need updating
    to_update = []
    any_match_found = False

    for i, variant in enumerate(shopify_variants):
        new_price = None
        ae_sku_id = variant_ae_map.get(variant["id"])

        if ae_sku_id and ae_sku_id in price_by_ae_sku:
            new_price = price_by_ae_sku[ae_sku_id]
        elif not matched_via_metafield:
            label = _variant_label(variant)
            new_price = _fuzzy_label_match(label)
            if new_price is None and i < len(aliexpress_skus):
                ae_sku = aliexpress_skus[i]
                ae_price = ae_sku.get("sale_price") or ae_sku.get("price")
                if ae_price is not None:
                    new_price = float(ae_price)

        if new_price is not None:
            any_match_found = True
            if abs(float(variant["price"]) - new_price) > 0.01:
                to_update.append((variant["id"], new_price, variant.get("option1", "?"), variant["price"]))

    if not any_match_found:
        print("[UPDATE] No price changes (no metafield/label/positional match found)")
        return "failed"

    if not to_update:
        print("[UPDATE] Price already up to date — no Shopify update needed")
        return "unchanged"

    # ── Per-variant PUT — preserves image_id, inventory_item_id, everything else ──
    success_count = 0
    for variant_id, new_price, option_label, old_price in to_update:
        try:
            r2 = requests.put(
                f"{_base()}/variants/{variant_id}.json",
                json={"variant": {"id": variant_id, "price": str(new_price)}},
                headers=_h(), timeout=20,
            )
            r2.raise_for_status()
            success_count += 1
            print(f"[UPDATE] {option_label}: {old_price} → {new_price}")
        except Exception as e:
            print(f"[UPDATE] Failed for variant {variant_id}: {e}")

    if success_count == 0:
        return "failed"

    print(f"[UPDATE] Success, {success_count}/{len(to_update)} variants updated")
    return "updated"




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



def update_shopify_product_inventory_with_skus(shopify_product_id: str, aliexpress_skus: list) -> bool:
    """
    Push AliExpress per-SKU stock to matching Shopify variants' inventory_quantity.
 
    IMPORTANT: Shopify's variant.inventory_quantity is the SUM across all
    locations. To avoid double-counting when a store has multiple locations,
    we set the full target quantity at the PRIMARY location and zero out
    every other location for that inventory item.
    """
    if not settings.SHOPIFY_STORE:
        return False
 
    stock_by_ae_sku = {}
    for sku in aliexpress_skus:
        ae_sku_id = str(sku.get("sku_id"))
        stock = sku.get("stock")
        if ae_sku_id and ae_sku_id != "None" and stock is not None:
            try:
                stock_by_ae_sku[ae_sku_id] = int(stock)
            except (ValueError, TypeError):
                continue
 
    if not stock_by_ae_sku and not aliexpress_skus:
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
            mf_res = requests.get(
                f"{_base()}/variants/{vid}/metafields.json",
                params={"namespace": "aliexpress", "key": "sku_id"},
                headers=_h(), timeout=10,
            )
            if mf_res.status_code == 200:
                mfs = mf_res.json().get("metafields", [])
                if mfs:
                    variant_ae_map[vid] = mfs[0].get("value")
        except Exception as e:
            print(f"[Inventory] Metafield error for variant {vid}: {e}")
 
    matched_via_metafield = any(v["id"] in variant_ae_map for v in shopify_variants)
 
    stock_by_label = {}
    for sku in aliexpress_skus:
        label = (sku.get("label") or sku.get("sku_attr") or "").strip().lower()
        stock = sku.get("stock")
        if label and stock is not None:
            try:
                stock_by_label[label] = int(stock)
            except (ValueError, TypeError):
                continue
 
    def _variant_label(variant: dict) -> str:
        parts = [
            variant.get(f"option{i}")
            for i in (1, 2, 3)
            if variant.get(f"option{i}") and variant.get(f"option{i}") != "Default Title"
        ]
        return " / ".join(parts).strip().lower()
 
    changes = False
    for i, variant in enumerate(shopify_variants):
        new_stock = None
        ae_sku_id = variant_ae_map.get(variant["id"])
 
        # 1. Metafield match (most reliable)
        if ae_sku_id and ae_sku_id in stock_by_ae_sku:
            new_stock = stock_by_ae_sku[ae_sku_id]

        # 2. Label fuzzy match
        elif not matched_via_metafield:
            label = _variant_label(variant)
            if label and label in stock_by_label:
                new_stock = stock_by_label[label]
            # 3. Positional fallback — same index in aliexpress_skus
            elif i < len(aliexpress_skus):
                ae_sku = aliexpress_skus[i]
                stock_val = ae_sku.get("stock")
                if stock_val is not None:
                    try:
                        new_stock = int(stock_val)
                    except (ValueError, TypeError):
                        new_stock = None
 
        if new_stock is None:
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
 
    return changes

def set_product_out_of_stock(shopify_product_id: str) -> bool:
    """
    Zero out inventory for ALL variants of a Shopify product across ALL locations.
    Used when an AliExpress listing is confirmed dead (no prices) and hasn't been
    remapped yet — we don't want to keep selling something we can no longer source.
    Does NOT touch price, images, or any other variant fields.
    """
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
            print(f"[DeadStock] No variants found for {shopify_product_id}")
            return False
    except Exception as e:
        print(f"[DeadStock] Fetch variants failed: {e}")
        return False

    try:
        loc_res = requests.get(f"{_base()}/locations.json", headers=_h(), timeout=15)
        loc_res.raise_for_status()
        locations = loc_res.json().get("locations", [])
    except Exception as e:
        print(f"[DeadStock] Failed to fetch locations: {e}")
        return False

    if not locations:
        print(f"[DeadStock] No Shopify locations found")
        return False

    success_count = 0
    for variant in variants:
        inventory_item_id = variant.get("inventory_item_id")
        if not inventory_item_id:
            continue
        variant_ok = True
        for loc in locations:
            try:
                res2 = requests.post(
                    f"{_base()}/inventory_levels/set.json",
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
        if variant_ok:
            success_count += 1

    print(f"[DeadStock] Zeroed inventory for {success_count}/{len(variants)} variant(s) of product {shopify_product_id}")
    return success_count > 0


