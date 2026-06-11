"""
AliExpress DS Dropshipping API client — fully corrected based on actual API response.

Key findings from debug endpoint:
- Shipping: logistics_info_dto.delivery_time (int, days) + ship_to_country
- Stock: sku.sku_available_stock (NOT ipm_sku_stock which is always null)
- Variant names: sku.ae_sku_property_dtos
- Sales count: ae_item_base_info_dto.sales_count (NOT lastest_volume)
- Extra data: package_info_dto, ae_store_info available
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


def _sign(secret: str, params: dict) -> str:
    keys     = sorted(params.keys())
    sign_str = secret + "".join(f"{k}{params[k]}" for k in keys) + secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


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
        err = body["error_response"]
        raise HTTPException(status_code=400, detail={
            "aliexpress_error_code": err.get("code", ""),
            "aliexpress_message":    err.get("msg") or err.get("message", ""),
            "raw": body,
        })


# ─────────────────────────────────────────────
# Variant name resolver
# ─────────────────────────────────────────────

def _resolve_sku_label(sku: dict) -> str:
    """
    Build a human-readable label from ae_sku_property_dtos.
    e.g. "Red / XL" instead of "5:100014065;14:200002987"
    """
    props = sku.get("ae_sku_property_dtos", {})
    if isinstance(props, dict):
        prop_list = props.get("ae_sku_property_d_t_o", [])
    elif isinstance(props, list):
        prop_list = props
    else:
        prop_list = []

    if not prop_list:
        # Fall back to raw sku_attr
        raw = sku.get("sku_attr", "")
        parts = [p.split(":")[-1].strip() for p in raw.split(";") if ":" in p]
        return " / ".join(parts) if parts else raw

    parts = []
    for prop in prop_list:
        # Use property_value_definition_name if available (has translated name)
        val = (
            prop.get("property_value_definition_name")
            or prop.get("attr_value")
            or prop.get("sku_property_value")
            or str(prop.get("attr_value_id", ""))
        )
        if val:
            parts.append(str(val).strip())

    return " / ".join(parts) if parts else sku.get("sku_attr", "")


# ─────────────────────────────────────────────
# Shipping parser — from logistics_info_dto
# ─────────────────────────────────────────────

def _parse_freight(result: dict) -> dict:
    """
    Extract shipping from logistics_info_dto.
    Confirmed fields: delivery_time (int days), ship_to_country (str)
    """
    logistics = result.get("logistics_info_dto", {})

    if logistics:
        days = logistics.get("delivery_time")
        country = logistics.get("ship_to_country", "")

        # Check for list of shipping options (some products have multiple)
        ae_logistics = logistics.get("ae_logistics_info", [])
        if isinstance(ae_logistics, list) and ae_logistics:
            # Pick cheapest option
            cheapest = None
            cheapest_price = float("inf")
            for item in ae_logistics:
                if not isinstance(item, dict):
                    continue
                price_info = item.get("freight_amount", {})
                if isinstance(price_info, dict):
                    try:
                        price_val = float(price_info.get("amount", 9999))
                    except (ValueError, TypeError):
                        price_val = 9999
                else:
                    price_val = 9999
                if price_val < cheapest_price:
                    cheapest_price = price_val
                    cheapest = item

            if cheapest:
                price_info = cheapest.get("freight_amount", {})
                amt = float(price_info.get("amount", 0)) if isinstance(price_info, dict) else 0
                est_days = cheapest.get("estimated_delivery_time") or cheapest.get("delivery_days")
                return {
                    "cost":   "Free" if amt == 0 else f"{price_info.get('currency','USD')} {amt:.2f}",
                    "method": cheapest.get("company", "Standard Shipping"),
                    "days":   str(est_days) if est_days else (str(days) if days else ""),
                }

        # Simple case: just delivery_time days
        if days is not None:
            return {
                "cost":   "Calculated at checkout",
                "method": "Standard Shipping",
                "days":   f"{days} days to {country}" if country else f"{days} days",
            }

    # ae_item_properties fallback
    props = result.get("ae_item_properties", {})
    freight_tpl = props.get("freight_template", {})
    if freight_tpl and isinstance(freight_tpl, dict):
        cost   = freight_tpl.get("freight")
        method = freight_tpl.get("delivery_time") or freight_tpl.get("company")
        d_days = freight_tpl.get("delivery_days")
        return {
            "cost":   "Free" if cost == 0 else (str(cost) if cost else "Calculated at checkout"),
            "method": str(method) if method else "Standard Shipping",
            "days":   str(d_days) if d_days else "",
        }

    return {
        "cost":   "Calculated at checkout",
        "method": "Standard Shipping",
        "days":   "",
    }


# ─────────────────────────────────────────────
# Inventory parser — using sku_available_stock
# ─────────────────────────────────────────────

def _parse_inventory(result: dict, sku_list: list) -> dict:
    """
    Correct field is sku_available_stock (confirmed from all_sku_fields in debug).
    ipm_sku_stock is always null for DS products.
    sales_count in base info is the order count (not lastest_volume).
    """
    base = result.get("ae_item_base_info_dto", {})

    # Sales count — confirmed field name from debug
    sales_count = None
    for field in ("sales_count", "lastest_volume", "volume"):
        val = base.get(field)
        if val:
            try:
                sales_count = int(str(val).replace("+", "").replace(",", ""))
                break
            except (ValueError, TypeError):
                pass

    # Build SKU inventory using the CORRECT field: sku_available_stock
    sku_inventory = []
    total_stock = 0
    any_stock = False

    for sku in sku_list:
        stock_val = sku.get("sku_available_stock")   # ← correct field
        label = _resolve_sku_label(sku)

        try:
            stock = int(stock_val) if stock_val is not None else None
        except (ValueError, TypeError):
            stock = None

        if stock is not None and stock > 0:
            total_stock += stock
            any_stock = True

        sku_inventory.append({
            "sku_id": sku.get("sku_id"),
            "attr":   label,
            "stock":  stock,
        })

    if any_stock:
        return {
            "total_stock":     total_stock,
            "stock_available": True,
            "stock_source":    "sku_available_stock",
            "sku_inventory":   sku_inventory,
            "sales_count":     sales_count,
            "note":            f"{total_stock} units across {len(sku_inventory)} SKU(s)",
        }

    # sku_available_stock also null/0 — truly hidden
    return {
        "total_stock":     None,
        "stock_available": None,
        "stock_source":    "unavailable",
        "sku_inventory":   sku_inventory,
        "sales_count":     sales_count,
        "note":            (
            "AliExpress DS API does not expose inventory for this product. "
            "This is normal for dropshipping — the supplier fulfils on demand."
        ),
    }


# ─────────────────────────────────────────────
# Product parser
# ─────────────────────────────────────────────

def _parse_product(raw: dict) -> dict:
    result = (
        raw
        .get("aliexpress_ds_product_get_response", {})
        .get("result", {})
    )
    if not result:
        return raw

    product  = result.get("ae_item_base_info_dto", {})
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

    # Package info (weight, dimensions) — present in API but ignored before
    pkg = result.get("package_info_dto", {})

    # Store info
    store = result.get("ae_store_info", {})

    # Build SKUs with resolved human-readable labels
    skus = []
    for sku in sku_list:
        skus.append({
            "sku_id":       sku.get("sku_id"),
            "sku_attr":     sku.get("sku_attr"),
            "label":        _resolve_sku_label(sku),   # human-readable e.g. "Red / XL"
            "price":        sku.get("sku_price"),
            "sale_price":   sku.get("offer_sale_price"),
            "bulk_price":   sku.get("offer_bulk_sale_price"),
            "stock":        sku.get("sku_available_stock"),   # CORRECT field
            "currency":     sku.get("currency_code"),
        })

    shipping  = _parse_freight(result)
    inventory = _parse_inventory(result, sku_list)

    return {
        # Identity
        "product_id":   product.get("product_id"),
        "title":        product.get("subject"),
        "category_id":  product.get("category_id"),
        "store_id":     store.get("store_id") or product.get("store_id"),
        "store_name":   store.get("store_name") or product.get("store_name"),

        # Pricing
        "original_price": product.get("original_price"),
        "sale_price":     product.get("sale_price"),
        "currency":       product.get("currency_code"),
        "discount":       product.get("discount"),

        # Media
        "main_image":  images.split(";")[0] if images else product.get("main_image"),
        "all_images":  images.split(";") if images else [],

        # Ratings & Sales
        "avg_rating":    product.get("avg_evaluation_rating"),
        "review_count":  product.get("evaluation_count"),
        "orders":        product.get("sales_count"),   # correct field name

        # Variants / SKUs
        "sku_count": len(skus),
        "skus":      skus,

        # Package info
        "package_weight":      pkg.get("gross_weight") or pkg.get("package_weight"),
        "package_length":      pkg.get("package_length"),
        "package_width":       pkg.get("package_width"),
        "package_height":      pkg.get("package_height"),

        # Links
        "product_url": f"https://www.aliexpress.com/item/{product.get('product_id')}.html",

        # Shipping (correctly parsed)
        "shipping_info": {
            "cost":   shipping["cost"],
            "method": shipping["method"],
            "days":   shipping["days"],
        },

        # Inventory
        "total_stock":     inventory["total_stock"],
        "stock_available": inventory["stock_available"],
        "stock_source":    inventory["stock_source"],
        "stock_note":      inventory["note"],
        "sku_inventory":   inventory["sku_inventory"],
        "sales_count":     inventory["sales_count"],
    }


def _parse_search(raw: dict) -> dict:
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
            "product_id":     item.get("product_id"),
            "title":          item.get("product_title"),
            "main_image":     item.get("product_main_image_url"),
            "sale_price":     item.get("target_sale_price"),
            "original_price": item.get("target_original_price"),
            "currency":       item.get("target_sale_price_currency"),
            "discount":       item.get("discount"),
            "orders":         item.get("lastest_volume"),
            "avg_rating":     item.get("evaluate_rate"),
            "store_id":       item.get("store_id"),
            "store_name":     item.get("store_name"),
            "product_url":    f"https://www.aliexpress.com/item/{item.get('product_id')}.html",
        })

    return {
        "total_count": result.get("total_count"),
        "page":        result.get("current_page_no"),
        "page_size":   result.get("current_record_count"),
        "products":    clean,
    }


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def get_product(product_id: str, db: Session = None) -> dict:
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
    """Shipping is embedded in the product response — no separate endpoint exists."""
    try:
        product = get_product(product_id, db)
        info = product.get("shipping_info", {})
        return {
            "shipping_cost": info.get("cost", "Calculated at checkout"),
            "method":        info.get("method", "Standard Shipping"),
            "days":          info.get("days", ""),
        }
    except Exception:
        return {
            "shipping_cost": "Calculated at checkout",
            "method":        "Standard Shipping",
            "days":          "",
        }