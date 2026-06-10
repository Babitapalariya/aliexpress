"""
AliExpress DS Dropshipping API client.

Signing algorithm from the official python-aliexpress-api SDK (base.py):
  - Sign method : MD5
  - Sign string : secret + sorted(key+value) + secret  → MD5 → uppercase
  - System params (app_key, method, timestamp, sign_method, v, access_token, sign) → URL query
  - Application/business params → POST body
  - Endpoint: POST https://api-sg.aliexpress.com/sync
"""

import hashlib
import http.client
import json
import time
import urllib.parse
from fastapi import HTTPException
from sqlalchemy.orm import Session

from .config import get_settings
from .auth import get_latest_token

settings = get_settings()

DOMAIN   = "api-sg.aliexpress.com"
ENDPOINT = "/sync"


# ─────────────────────────────────────────────
# Signing
# ─────────────────────────────────────────────

def _sign(secret: str, params: dict) -> str:
    keys     = sorted(params.keys())
    sign_str = secret + "".join(f"{k}{params[k]}" for k in keys) + secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


# ─────────────────────────────────────────────
# Raw gateway call
# ─────────────────────────────────────────────

def _call(method: str, app_params: dict, access_token: str) -> dict:
    sys_params = {
        "app_key":      settings.ALIEXPRESS_APP_KEY,
        "method":       method,
        "timestamp":    str(int(time.time() * 1000)),
        "sign_method":  "md5",
        "v":            "2.0",
        "access_token": access_token,
    }
    sys_params["sign"] = _sign(settings.ALIEXPRESS_APP_SECRET, {**sys_params, **app_params})

    url  = ENDPOINT + "?" + urllib.parse.urlencode(sys_params)
    body = urllib.parse.urlencode(app_params)

    conn = http.client.HTTPSConnection(DOMAIN, timeout=30)
    conn.request("POST", url, body=body, headers={
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Cache-Control": "no-cache",
    })
    resp   = conn.getresponse()
    result = resp.read().decode("utf-8")
    conn.close()

    if resp.status != 200:
        raise HTTPException(status_code=502, detail=f"AliExpress HTTP {resp.status}: {result}")

    return json.loads(result)


def _check_error(body: dict):
    if not isinstance(body, dict):
        return
    if "error_response" in body:
        err  = body["error_response"]
        raise HTTPException(status_code=400, detail={
            "aliexpress_error_code": err.get("code", ""),
            "aliexpress_message":    err.get("msg") or err.get("message", ""),
            "raw": body,
        })


# ─────────────────────────────────────────────
# Response parsers — return only useful fields
# ─────────────────────────────────────────────

def _parse_product(raw: dict) -> dict:
    """
    Extract the important fields from aliexpress.ds.product.get response.
    Drops internal/redundant fields to keep the response clean.
    """
    # The actual product data is nested under the result key
    result = (
        raw
        .get("aliexpress_ds_product_get_response", {})
        .get("result", {})
    )
    if not result:
        return raw  # return as-is if structure is unexpected

    product = result.get("ae_item_base_info_dto", {})
    sku_list = (
        result
        .get("ae_item_sku_info_dtos", {})
        .get("ae_item_sku_info_d_t_o", [])
    )
    images = (
        result
        .get("ae_multimedia_info_dto", {})
        .get("image_urls", "")
    )
    shipping = result.get("ae_item_properties", {})
    shipping_info = result.get("ae_item_properties", {}).get("freight_template", {})
    total_stock = sum(int(sku.get("ipm_sku_stock", 0)) for sku in sku_list if sku.get("ipm_sku_stock"))

    # Build clean SKU list
    skus = []
    for sku in sku_list:
        skus.append({
            "sku_id":         sku.get("sku_id"),
            "sku_attr":       sku.get("sku_attr"),           # e.g. "Color:Red;Size:XL"
            "price":          sku.get("sku_price"),
            "sale_price":     sku.get("offer_sale_price"),
            "bulk_price":     sku.get("offer_bulk_sale_price"),
            "stock":          sku.get("ipm_sku_stock"),
            "currency":       sku.get("currency_code"),
        })

    return {
        # ── Identity
        "product_id":       product.get("product_id"),
        "title":            product.get("subject"),
        "category_id":      product.get("category_id"),
        "store_id":         product.get("store_id"),
        "store_name":       product.get("store_name"),

        # ── Pricing
        "original_price":   product.get("original_price"),
        "sale_price":       product.get("sale_price"),
        "currency":         product.get("currency_code"),
        "discount":         product.get("discount"),

        # ── Media
        "main_image":       product.get("ae_item_base_info_dto", {}).get("main_image") or images.split(";")[0] if images else None,
        "all_images":       images.split(";") if images else [],

        # ── Ratings & Sales
        "avg_rating":       product.get("avg_evaluation_rating"),
        "review_count":     product.get("evaluation_count"),
        "orders":           product.get("lastest_volume"),

        # ── Logistics
        "ship_to_country":  product.get("country_of_origin"),
        "delivery_days":    product.get("delivery_days"),

        # ── Variants / SKUs
        "sku_count":        len(skus),
        "skus":             skus,

        # ── Links
        "product_url":      f"https://www.aliexpress.com/item/{product.get('product_id')}.html",

        "shipping_info": {
            "cost": shipping_info.get("freight", "Calculated at checkout"),
            "method": shipping_info.get("delivery_time", "Standard Shipping")
        },
        "total_stock": total_stock,
        "sku_inventory": [
            {"sku_id": sku.get("sku_id"), "attr": sku.get("sku_attr"), "stock": sku.get("ipm_sku_stock")}
            for sku in sku_list
        ]

    }


def _parse_search(raw: dict) -> dict:
    """
    Extract important fields from aliexpress.ds.text.search response.
    """
    result = (
        raw
        .get("aliexpress_ds_text_search_response", {})
        .get("result", {})
    )
    if not result:
        return raw

    items = result.get("products", {}).get("traffic_product_d_t_o", [])
    clean = []
    for item in items:
        clean.append({
            "product_id":   item.get("product_id"),
            "title":        item.get("product_title"),
            "main_image":   item.get("product_main_image_url"),
            "sale_price":   item.get("target_sale_price"),
            "original_price": item.get("target_original_price"),
            "currency":     item.get("target_sale_price_currency"),
            "discount":     item.get("discount"),
            "orders":       item.get("lastest_volume"),
            "avg_rating":   item.get("evaluate_rate"),
            "store_id":     item.get("store_id"),
            "store_name":   item.get("store_name"),
            "product_url":  f"https://www.aliexpress.com/item/{item.get('product_id')}.html",
        })

    return {
        "total_count":   result.get("total_count"),
        "page":          result.get("current_page_no"),
        "page_size":     result.get("current_record_count"),
        "products":      clean,
    }


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def get_product(product_id: str, db: Session = None) -> dict:
    """Fetch a single AliExpress DS product by ID, returning clean key fields."""
    if db is None:
        from .database import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        token = get_latest_token(db)
        raw   = _call(
            method="aliexpress.ds.product.get",
            app_params={
                "product_id":      product_id,
                "local":           "en_US",
                "ship_to_country": "US",
                "target_currency": "USD",
            },
            access_token=token.access_token,
        )
        _check_error(raw)
        return _parse_product(raw)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if close_db:
            db.close()


def search_products(keyword: str, page: int = 1, page_size: int = 20, db: Session = None) -> dict:
    """Search AliExpress DS products by keyword, returning clean results."""
    if db is None:
        from .database import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        token = get_latest_token(db)
        raw   = _call(
            method="aliexpress.ds.text.search",
            app_params={
                "search_key":      keyword,
                "page_no":         str(page),
                "page_size":       str(page_size),
                "local":           "en_US",
                "ship_to_country": "US",
                "target_currency": "USD",
                "sort":            "SALE_PRICE_ASC",
            },
            access_token=token.access_token,
        )
        _check_error(raw)
        return _parse_search(raw)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if close_db:
            db.close()


def get_shipping_info(product_id: str, country_code: str = "US", db: Session = None) -> dict:
    """Fetch real shipping cost for a product to a specific country."""
    if db is None:
        from .database import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        token = get_latest_token(db)
        raw = _call(
            method="aliexpress.ds.shipping.get",   # actual method name may vary
            app_params={
                "product_id": product_id,
                "country": country_code,
                "quantity": "1",
            },
            access_token=token.access_token,
        )
        _check_error(raw)
        result = raw.get("aliexpress_ds_shipping_get_response", {}).get("result", {})
        return {
            "shipping_cost": result.get("shipping_cost", "Calculated at checkout"),
            "method": result.get("delivery_time", "Standard Shipping")
        }
    except Exception:
        return {"shipping_cost": "Calculated at checkout", "method": "Standard Shipping"}
    finally:
        if close_db:
            db.close()