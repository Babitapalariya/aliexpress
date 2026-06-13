# main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import math
import time
import re
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from .models import ProductMapping

from .database import get_db, engine
from .models import ImportedProduct, Base
from .auth import router as auth_router, get_latest_token
from .aliexpress import get_product
from .shopify import (
    create_shopify_product,
    check_product_exists_in_shopify,
    update_shopify_product_price,
    get_shopify_product_by_aliexpress_id,
    get_all_shopify_imported_products,
    get_shopify_token,
    update_shopify_product_prices_with_skus,
    store_aliexpress_sku_ids, 
    increase_shopify_product_price, 
)
from .config import get_settings
from .aliexpress import get_product, get_shipping_info



settings = get_settings()
Base.metadata.create_all(bind=engine)

scheduler = None

# ─────────────────────────────────────────────
# PRICE SYNC HELPERS
# ─────────────────────────────────────────────




# def sync_product_price(product_id: int, db: Session) -> bool:
#     product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
#     if not product or not product.track_price or not product.aliexpress_id:
#         return False

#     try:
#         latest_data = get_product(product.aliexpress_id, db)
#     except Exception as e:
#         print(f"[Sync] Failed to fetch product {product.aliexpress_id}: {e}")
#         return False

#     new_skus = latest_data.get("skus", [])
#     if not new_skus:
#         print(f"[Sync] No SKUs for {product.aliexpress_id}")
#         return False

#     # Apply price increase to each SKU
#     increase = product.price_increase or 0.0
#     if increase != 0.0:
#         for sku in new_skus:
#             current_price = sku.get("sale_price") or sku.get("price")
#             if current_price is not None:
#                 new_price = float(current_price) + increase
#                 sku["sale_price"] = str(new_price)
#                 sku["price"] = str(new_price)

#     shopify_updated = False
#     if product.shopify_product_id:
#         shopify_updated = update_shopify_product_prices_with_skus(
#             product.shopify_product_id, new_skus
#         )

#     # Update local original price (without increase)
#     original_price = latest_data.get("sale_price") or latest_data.get("original_price")
#     if original_price:
#         product.original_price = original_price
#         if not product.custom_price:
#             product.custom_price = None
#     db.commit()

#     print(f"[Sync] Product {product.aliexpress_id} processed. Shopify updated: {shopify_updated}")
#     return shopify_updated

def sync_product_price(product_id: int, db: Session) -> bool:
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product or not product.track_price or not product.aliexpress_id:
        return False

    # Skip if manual mode – user has fixed price
    if product.price_mode == 'manual':
        print(f"[Sync] Skipping manual product {product.aliexpress_id}")
        return False

    try:
        latest_data = get_product(product.aliexpress_id, db)
    except Exception as e:
        print(f"[Sync] Failed to fetch product {product.aliexpress_id}: {e}")
        return False

    new_skus = latest_data.get("skus", [])
    if not new_skus:
        print(f"[Sync] No SKUs for {product.aliexpress_id}")
        return False

    # Apply increase if mode is 'increase'
    if product.price_mode == 'increase' and product.price_increase != 0.0:
        for sku in new_skus:
            current_price = sku.get("sale_price") or sku.get("price")
            if current_price is not None:
                new_price = float(current_price) + product.price_increase
                sku["sale_price"] = str(new_price)
                sku["price"] = str(new_price)

    shopify_updated = False
    if product.shopify_product_id:
        shopify_updated = update_shopify_product_prices_with_skus(
            product.shopify_product_id, new_skus
        )

    # Update local original price
    original_price = latest_data.get("sale_price") or latest_data.get("original_price")
    if original_price:
        product.original_price = original_price

    db.commit()
    print(f"[Sync] Product {product.aliexpress_id} processed (mode={product.price_mode}). Shopify updated: {shopify_updated}")
    return shopify_updated

def sync_existing_shopify_products_to_db():
    """Fetch all Shopify products with tag 'aliexpress-import' and create local DB records if missing."""
    db = SessionLocal()
    try:
        shopify_products = get_all_shopify_imported_products()
        for sp in shopify_products:
            aliexpress_id = sp.get("aliexpress_id")
            if not aliexpress_id:
                print(f"[Sync] Skipping Shopify product {sp['shopify_id']} - no aliexpress_id metafield")
                continue

            existing = db.query(ImportedProduct).filter(ImportedProduct.aliexpress_id == aliexpress_id).first()
            if existing:
                if not existing.shopify_product_id:
                    existing.shopify_product_id = sp["shopify_id"]
                    existing.shopify_status = sp["status"]
                    db.commit()
                continue

            # Fetch full product data from AliExpress
            try:
                raw_product = get_product(aliexpress_id, db)
            except Exception as e:
                print(f"[Sync] Failed to fetch AliExpress product {aliexpress_id}: {e}")
                continue

            new_product = ImportedProduct(
                aliexpress_id=aliexpress_id,
                original_title=raw_product.get("title") or sp["title"],
                original_price=raw_product.get("sale_price") or raw_product.get("original_price"),
                currency=raw_product.get("currency"),
                main_image=raw_product.get("main_image"),
                all_images=raw_product.get("all_images"),
                store_name=raw_product.get("store_name"),
                avg_rating=raw_product.get("avg_rating"),
                review_count=raw_product.get("review_count"),
                orders=raw_product.get("orders"),
                sku_count=raw_product.get("sku_count"),
                skus=raw_product.get("skus"),
                shopify_product_id=sp["shopify_id"],
                shopify_status=sp["status"],
                track_price=True,
            )
            db.add(new_product)
            db.commit()
            print(f"[Sync] Added local record for product {aliexpress_id} (Shopify ID {sp['shopify_id']})")
    except Exception as e:
        print(f"[Sync] Error syncing Shopify products: {e}")
    finally:
        db.close()


def sync_all_tracked_products():
    """Background task: first import existing Shopify products, then sync prices."""
    sync_existing_shopify_products_to_db()
    db = SessionLocal()
    try:
        products = db.query(ImportedProduct).filter(ImportedProduct.track_price == True).all()
        print(f"[Sync] Starting price sync for {len(products)} products")
        for p in products:
            sync_product_price(p.id, db)
    except Exception as e:
        print(f"[Sync] Background task error: {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────
# SCHEDULER LIFESPAN
# ─────────────────────────────────────────────
# def start_scheduler():
#     global scheduler
#     scheduler = BackgroundScheduler()
#     scheduler.add_job(
#         sync_all_tracked_products,
#         trigger=IntervalTrigger(hours=1),
#         id="price_sync_job",
#         replace_existing=True,
#     )
#     scheduler.start()
#     print("[Scheduler] Started – will sync prices every hour")

def start_scheduler():
    global scheduler
    scheduler = BackgroundScheduler()
    # Existing job for imported products
    scheduler.add_job(
        sync_all_tracked_products,
        trigger=IntervalTrigger(hours=1),
        id="price_sync_job",
        replace_existing=True,
    )
    # NEW job for mapped products
    scheduler.add_job(
        sync_all_mapped_products_background,
        trigger=IntervalTrigger(hours=1),
        id="mapped_price_sync_job",
        replace_existing=True,
    )
    scheduler.start()
    print("[Scheduler] Started – will sync prices every hour for both imported and mapped products")        

def shutdown_scheduler():
    if scheduler:
        scheduler.shutdown()
        print("[Scheduler] Stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    # Run once at startup to pull existing Shopify products into DB
    sync_existing_shopify_products_to_db()
    yield
    shutdown_scheduler()


app = FastAPI(title="AliShopify Backend", lifespan=lifespan)

# CORS
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

app.include_router(auth_router)

#app.include_router(auth_router, prefix="/api")




from fastapi import FastAPI as _RootFastAPI
from fastapi.middleware.cors import CORSMiddleware as _RootCORS
 
root_app = _RootFastAPI(title="AliShopify Backend Root")
 
# Re-apply CORS on the root app as well (covers the mount boundary)
root_app.add_middleware(
    _RootCORS,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
 
# Mount your existing fully-configured app under /api
root_app.mount("/api", app)



from .database import SessionLocal


# ─────────────────────────────────────────────
# DASHBOARD ENDPOINTS (unchanged)
# ─────────────────────────────────────────────
@app.get("/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db)):
    total = db.query(ImportedProduct).count()
    draft = db.query(ImportedProduct).filter(ImportedProduct.shopify_status == "draft").count()
    active = db.query(ImportedProduct).filter(ImportedProduct.shopify_status == "active").count()
    modified = db.query(ImportedProduct).filter(
        (ImportedProduct.custom_title.isnot(None)) |
        (ImportedProduct.custom_price.isnot(None)) |
        (ImportedProduct.custom_rating.isnot(None)) |
        (ImportedProduct.custom_description.isnot(None))
    ).count()
    return {"total_imported": total, "draft": draft, "active": active, "modified": modified}


@app.get("/dashboard/products")
def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db)
):
    query = db.query(ImportedProduct)
    total = query.count()
    pages = math.ceil(total / page_size) if total > 0 else 1
    offset = (page - 1) * page_size
    products = query.order_by(ImportedProduct.imported_at.desc()).offset(offset).limit(page_size).all()

    result = []
    for p in products:
        result.append({
            "id": p.id,
            "aliexpress_id": p.aliexpress_id,
            "title": p.custom_title or p.original_title,
            "original_title": p.original_title,
            "price": p.custom_price or p.original_price,
            "currency": p.currency,
            "main_image": p.main_image,
            "store_name": p.store_name,
            "avg_rating": p.custom_rating or p.avg_rating,
            "rating": p.custom_rating or p.avg_rating,
            "sku_count": p.sku_count,
            "shopify_status": p.shopify_status,
            "shopify_product_id": p.shopify_product_id,
            "imported_at": p.imported_at.isoformat() if p.imported_at else None,
            "custom_title": p.custom_title,
            "custom_price": p.custom_price,
            "custom_rating": p.custom_rating,
            "custom_description": p.custom_description,
            "track_price": p.track_price,
            "price_mode": p.price_mode,
            "price_increase": p.price_increase,
        })
    return {"products": result, "total": total, "page": page, "pages": pages}


# @app.get("/dashboard/products/{product_id}")
# def get_single_product(product_id: int, db: Session = Depends(get_db)):
#     p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
#     if not p:
#         raise HTTPException(status_code=404, detail="Product not found")
#     return {
#         "id": p.id,
#         "aliexpress_id": p.aliexpress_id,
#         "title": p.custom_title or p.original_title,
#         "original_title": p.original_title,
#         "price": p.custom_price or p.original_price,
#         "currency": p.currency,
#         "main_image": p.main_image,
#         "store_name": p.store_name,
#         "avg_rating": p.custom_rating or p.avg_rating,
#         "sku_count": p.sku_count,
#         "shopify_status": p.shopify_status,
#         "shopify_product_id": p.shopify_product_id,
#         "imported_at": p.imported_at.isoformat() if p.imported_at else None,
#         "custom_title": p.custom_title,
#         "custom_price": p.custom_price,
#         "custom_rating": p.custom_rating,
#         "custom_description": p.custom_description,
#         "track_price": p.track_price,
#     }




@app.put("/dashboard/products/{product_id}")
def update_product(product_id: int, payload: dict, db: Session = Depends(get_db)):
    p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")

    if "custom_title" in payload:
        p.custom_title = payload["custom_title"] or None
    if "custom_price" in payload:
        p.custom_price = payload["custom_price"] or None
    if "custom_rating" in payload:
        try:
            val = float(payload["custom_rating"]) if payload["custom_rating"] else None
            p.custom_rating = val
        except:
            p.custom_rating = None
    if "custom_description" in payload:
        p.custom_description = payload["custom_description"] or None
    if "track_price" in payload:
        p.track_price = bool(payload["track_price"])

    db.commit()
    db.refresh(p)

    shopify_synced = False
    if p.shopify_product_id:
        try:
            from .shopify import update_shopify_product
            update_shopify_product(p.shopify_product_id, {
                "title": p.custom_title or p.original_title,
                "price": p.custom_price or p.original_price,
                "body_html": p.custom_description or p.original_description or "",
                "rating": p.custom_rating or p.avg_rating,
            })
            shopify_synced = True
        except Exception as e:
            print(f"Shopify sync failed: {e}")
            shopify_synced = False
    else:
        shopify_synced = None

    return {
        "message": "Product updated",
        "shopify_synced": shopify_synced,
        "product": {
            "id": p.id,
            "title": p.custom_title or p.original_title,
            "price": p.custom_price or p.original_price,
        }
    }


@app.delete("/dashboard/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(p)
    db.commit()
    return {"message": "Deleted from local database"}


# ─────────────────────────────────────────────
# PRICE TRACKING TOGGLE & MANUAL SYNC
# ─────────────────────────────────────────────
@app.post("/dashboard/products/{product_id}/toggle-track-price")
def toggle_track_price(product_id: int, db: Session = Depends(get_db)):
    p = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not p:
        raise HTTPException(404, "Product not found")
    p.track_price = not p.track_price
    db.commit()
    return {"id": p.id, "track_price": p.track_price}


@app.post("/dashboard/sync-now")
def manual_sync(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    products = db.query(ImportedProduct).filter(ImportedProduct.track_price == True).all()
    def run_sync():
        for p in products:
            sync_product_price(p.id, SessionLocal())
    background_tasks.add_task(run_sync)
    return {"message": f"Price sync started for {len(products)} products"}


# ─────────────────────────────────────────────
# ALIEXPRESS & IMPORT ENDPOINT (with duplicate prevention)
# ─────────────────────────────────────────────
# @app.get("/product/{aliexpress_id}")
# def fetch_aliexpress_product(aliexpress_id: str, db: Session = Depends(get_db)):
#     get_latest_token(db)
#     product = get_product(aliexpress_id, db)
#     return product

@app.get("/product/{aliexpress_id}")
def fetch_aliexpress_product(aliexpress_id: str, db: Session = Depends(get_db)):
    get_latest_token(db)
    product = get_product(aliexpress_id, db)
    # Ensure shipping_info exists with 'cost' and 'method'
    if "shipping_info" not in product or not product["shipping_info"]:
        product["shipping_info"] = {"cost": "Calculated at checkout", "method": "Standard Shipping"}
    elif "cost" not in product["shipping_info"]:
        product["shipping_info"]["cost"] = "Calculated at checkout"
    return product




@app.post("/import/{aliexpress_id}")
def import_to_shopify(aliexpress_id: str, db: Session = Depends(get_db)):
    # 1. Check if this ID is already in product_mappings table
    mapping_exists = db.query(ProductMapping).filter(ProductMapping.aliexpress_id == aliexpress_id).first()
    if mapping_exists:
        raise HTTPException(
            status_code=409,
            detail=f"AliExpress ID {aliexpress_id} is already mapped to Shopify product {mapping_exists.shopify_product_id}. "
                   f"Use the 'Sync Mappings' page to update its price – do not import again."
        )

    # 2. Check local DB
    existing = db.query(ImportedProduct).filter(ImportedProduct.aliexpress_id == aliexpress_id).first()
    if existing and existing.shopify_product_id:
        raise HTTPException(409, "Product already imported to Shopify")

    # 3. Fetch fresh data from AliExpress
    raw_product = get_product(aliexpress_id, db)

    # 4. If not in local DB, check if product already exists in Shopify (by title)
    title = raw_product.get("title")
    if title and check_product_exists_in_shopify(title):
        # Find existing Shopify product ID
        shop = settings.SHOPIFY_STORE.replace(".myshopify.com", "").strip()
        token = get_shopify_token()
        try:
            res = requests.get(
                f"https://{shop}.myshopify.com/admin/api/{settings.SHOPIFY_API_VERSION}/products.json",
                params={"title": title, "limit": 1, "fields": "id,title,status"},
                headers={"X-Shopify-Access-Token": token},
                timeout=15
            )
            res.raise_for_status()
            products = res.json().get("products", [])
            if products:
                shopify_id = str(products[0]["id"])
                shopify_status = products[0].get("status", "draft")
                # Create local record only (no new Shopify product)
                if existing:
                    existing.shopify_product_id = shopify_id
                    existing.shopify_status = shopify_status
                    existing.original_title = raw_product.get("title")
                    existing.original_price = raw_product.get("sale_price") or raw_product.get("original_price")
                    existing.currency = raw_product.get("currency")
                    existing.main_image = raw_product.get("main_image")
                    existing.store_name = raw_product.get("store_name")
                    existing.avg_rating = raw_product.get("avg_rating")
                    existing.sku_count = raw_product.get("sku_count")
                    existing.skus = raw_product.get("skus")
                    existing.all_images = raw_product.get("all_images")
                    existing.track_price = True
                    db.commit()
                    db.refresh(existing)
                    product_id = existing.id
                else:
                    new_product = ImportedProduct(
                        aliexpress_id=aliexpress_id,
                        original_title=raw_product.get("title"),
                        original_price=raw_product.get("sale_price") or raw_product.get("original_price"),
                        currency=raw_product.get("currency"),
                        main_image=raw_product.get("main_image"),
                        all_images=raw_product.get("all_images"),
                        store_name=raw_product.get("store_name"),
                        avg_rating=raw_product.get("avg_rating"),
                        review_count=raw_product.get("review_count"),
                        orders=raw_product.get("orders"),
                        sku_count=raw_product.get("sku_count"),
                        skus=raw_product.get("skus"),
                        shopify_product_id=shopify_id,
                        shopify_status=shopify_status,
                        track_price=True,
                    )
                    db.add(new_product)
                    db.commit()
                    db.refresh(new_product)
                    product_id = new_product.id
                return {
                    "message": "Product already existed in Shopify – local record created/updated. No duplicate created.",
                    "product_id": product_id,
                    "shopify_product": {"id": shopify_id, "title": title, "status": shopify_status}
                }
        except Exception as e:
            print(f"Error checking Shopify product: {e}")
            raise HTTPException(409, f"Product '{title}' already exists in Shopify (title match), but could not retrieve its ID. Please contact support.")

    # 5. If not found in Shopify, create new product
    shopify_resp = create_shopify_product(raw_product)
    shopify_product_new = shopify_resp.get("product", {})
    shopify_id = str(shopify_product_new.get("id"))

    if existing:
        existing.shopify_product_id = shopify_id
        existing.shopify_status = shopify_product_new.get("status", "draft")
        existing.original_title = raw_product.get("title")
        existing.original_price = raw_product.get("sale_price") or raw_product.get("original_price")
        existing.currency = raw_product.get("currency")
        existing.main_image = raw_product.get("main_image")
        existing.store_name = raw_product.get("store_name")
        existing.avg_rating = raw_product.get("avg_rating")
        existing.review_count = raw_product.get("review_count")
        existing.orders = raw_product.get("orders")
        existing.sku_count = raw_product.get("sku_count")
        existing.skus = raw_product.get("skus")
        existing.all_images = raw_product.get("all_images")
        existing.track_price = True
        db.commit()
        db.refresh(existing)
        product_id = existing.id
    else:
        new_product = ImportedProduct(
            aliexpress_id=aliexpress_id,
            original_title=raw_product.get("title"),
            original_price=raw_product.get("sale_price") or raw_product.get("original_price"),
            currency=raw_product.get("currency"),
            main_image=raw_product.get("main_image"),
            all_images=raw_product.get("all_images"),
            store_name=raw_product.get("store_name"),
            avg_rating=raw_product.get("avg_rating"),
            review_count=raw_product.get("review_count"),
            orders=raw_product.get("orders"),
            sku_count=raw_product.get("sku_count"),
            skus=raw_product.get("skus"),
            shopify_product_id=shopify_id,
            shopify_status=shopify_product_new.get("status", "draft"),
            track_price=True,
        )
        db.add(new_product)
        db.commit()
        db.refresh(new_product)
        product_id = new_product.id

    return {
        "message": "Imported successfully",
        "product_id": product_id,
        "shopify_product": {
            "id": shopify_id,
            "title": shopify_product_new.get("title"),
            "status": shopify_product_new.get("status"),
        }
    }
    # main.py – add after existing endpoints

# ─────────────────────────────────────────────
# PRODUCT MAPPING ENDPOINTS
# ─────────────────────────────────────────────

@app.post("/mappings/add")
def add_mapping(aliexpress_id: str, shopify_product_id: str, shopify_product_title: str = None, db: Session = Depends(get_db)):
    existing = db.query(ProductMapping).filter(ProductMapping.aliexpress_id == aliexpress_id).first()
    if existing:
        raise HTTPException(400, "Mapping for this AliExpress ID already exists")
    mapping = ProductMapping(
        aliexpress_id=aliexpress_id,
        shopify_product_id=shopify_product_id,
        shopify_product_title=shopify_product_title,
        track_price=True,
        price_mode="auto",
        price_increase=0.0
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return {
        "message": "Mapping added",
        "mapping": {
            "id": mapping.id,
            "aliexpress_id": mapping.aliexpress_id,
            "shopify_product_id": mapping.shopify_product_id,
            "title": mapping.shopify_product_title,
            "track_price": mapping.track_price,
            "price_mode": mapping.price_mode,
            "price_increase": mapping.price_increase
        }
    }

@app.get("/mappings/list")
def list_mappings(db: Session = Depends(get_db)):
    mappings = db.query(ProductMapping).all()
    return [{
        "id": m.id,
        "aliexpress_id": m.aliexpress_id,
        "shopify_product_id": m.shopify_product_id,
        "title": m.shopify_product_title,
        "track_price": m.track_price,
        "price_mode": m.price_mode,
        "price_increase": m.price_increase
    } for m in mappings]

@app.delete("/mappings/{mapping_id}")
def delete_mapping(mapping_id: int, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    db.delete(mapping)
    db.commit()
    return {"message": "Mapping deleted"}

# @app.post("/mappings/sync-price")
# def sync_mapped_product_price(aliexpress_id: str, db: Session = Depends(get_db)):
#     mapping = db.query(ProductMapping).filter(ProductMapping.aliexpress_id == aliexpress_id).first()
#     if not mapping or not mapping.track_price:
#         raise HTTPException(404, "Mapping not found or price tracking disabled")

#     get_latest_token(db)

#     try:
#         from .aliexpress import get_product
#         raw = get_product(aliexpress_id, db)
#     except HTTPException as e:
#         raise HTTPException(status_code=e.status_code, detail=f"AliExpress API error: {e.detail}")
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to fetch from AliExpress: {str(e)}")

#     # Get SKUs from AliExpress response
#     aliexpress_skus = raw.get("skus", [])
#     if not aliexpress_skus:
#         raise HTTPException(500, "No SKUs found in AliExpress product")

#     # Update Shopify variant prices using SKU matching
#     from .shopify import update_shopify_product_prices_with_skus
#     success = update_shopify_product_prices_with_skus(mapping.shopify_product_id, aliexpress_skus)

#     if not success:
#         raise HTTPException(502, "Failed to update Shopify variant prices (no matching variants or API error)")

#     return {"message": "Variant prices updated successfully", "product_id": mapping.shopify_product_id}




@app.post("/mappings/sync-price")
def sync_mapped_product_price(aliexpress_id: str, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.aliexpress_id == aliexpress_id).first()
    if not mapping or not mapping.track_price:
        raise HTTPException(404, "Mapping not found or price tracking disabled")

    # If manual mode, do not sync (keep as is)
    if mapping.price_mode == "manual":
        raise HTTPException(400, "Mapping is in manual mode – use 'Set Price' to change")

    get_latest_token(db)

    try:
        raw = get_product(aliexpress_id, db)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch from AliExpress: {str(e)}")

    aliexpress_skus = raw.get("skus", [])
    if not aliexpress_skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    # If increase mode, apply stored increase
    if mapping.price_mode == "increase" and mapping.price_increase != 0.0:
        for sku in aliexpress_skus:
            price = sku.get("sale_price") or sku.get("price")
            if price:
                sku["sale_price"] = str(float(price) + mapping.price_increase)
                sku["price"] = str(float(price) + mapping.price_increase)

    from .shopify import update_shopify_product_prices_with_skus
    success = update_shopify_product_prices_with_skus(mapping.shopify_product_id, aliexpress_skus)

    if not success:
        raise HTTPException(502, "Failed to update Shopify variant prices")

    # **RESET mode to auto after manual sync**
    mapping.price_mode = "auto"
    mapping.price_increase = 0.0
    db.commit()

    return {"message": "Variant prices updated and mode reset to Auto", "product_id": mapping.shopify_product_id, "price_mode": "auto"}



@app.post("/mappings/sync-all")
def sync_all_mapped_products(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Background sync for all mapped products that have track_price=True."""
    mappings = db.query(ProductMapping).filter(ProductMapping.track_price == True).all()
    if not mappings:
        return {"message": "No mapped products to sync"}
    
    def run():
        from .database import SessionLocal
        sub_db = SessionLocal()
        try:
            for m in mappings:
                try:
                    # Make sure token is fresh for each call? We'll rely on get_product's internal token check.
                    raw = get_product(m.aliexpress_id, sub_db)
                    new_price = raw.get("sale_price") or raw.get("original_price")
                    if new_price:
                        update_shopify_product_price(m.shopify_product_id, float(new_price))
                        print(f"[SyncMapped] Updated price for {m.aliexpress_id} → {new_price}")
                    else:
                        print(f"[SyncMapped] No price for {m.aliexpress_id}")
                except Exception as e:
                    print(f"[SyncMapped] Error for {m.aliexpress_id}: {e}")
        finally:
            sub_db.close()
    
    background_tasks.add_task(run)
    return {"message": f"Started price sync for {len(mappings)} mapped products"}


# @app.post("/mappings/{mapping_id}/toggle-track")
# def toggle_mapping_track(mapping_id: int, db: Session = Depends(get_db)):
#     mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
#     if not mapping:
#         raise HTTPException(404, "Mapping not found")
#     mapping.track_price = not mapping.track_price
#     db.commit()
#     return {"id": mapping.id, "track_price": mapping.track_price}

@app.post("/mappings/{mapping_id}/toggle-track")
def toggle_track(mapping_id: int, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    mapping.track_price = not mapping.track_price
    db.commit()
    db.refresh(mapping)
    return {"id": mapping.id, "track_price": mapping.track_price}


def sync_all_mapped_products_background():
    from .database import SessionLocal
    db = SessionLocal()
    try:
        mappings = db.query(ProductMapping).filter(ProductMapping.track_price == True).all()
        for m in mappings:
            if m.price_mode == "manual":
                continue
            try:
                raw = get_product(m.aliexpress_id, db)
                skus = raw.get("skus", [])
                if not skus:
                    continue
                if m.price_mode == "increase" and m.price_increase != 0.0:
                    for sku in skus:
                        price = sku.get("sale_price") or sku.get("price")
                        if price:
                            sku["sale_price"] = str(float(price) + m.price_increase)
                            sku["price"] = str(float(price) + m.price_increase)
                update_shopify_product_prices_with_skus(m.shopify_product_id, skus)
                print(f"[Hourly] Updated variant prices for {m.aliexpress_id} (mode={m.price_mode})")
            except Exception as e:
                print(f"[Hourly] Error for {m.aliexpress_id}: {e}")
    finally:
        db.close()



@app.get("/debug/product/{shopify_product_id}")
def debug_product_prices(shopify_product_id: str, db: Session = Depends(get_db)):
    from .shopify import _base, _h
    res = requests.get(f"{_base()}/products/{shopify_product_id}.json", params={"fields": "id,title,variants"}, headers=_h())
    if res.status_code != 200:
        raise HTTPException(502, res.text)
    product = res.json().get("product", {})
    return {
        "shopify_id": product["id"],
        "title": product["title"],
        "variants": [{"id": v["id"], "option1": v.get("option1"), "price": v["price"]} for v in product.get("variants", [])]
    }

# @app.post("/dashboard/products/{product_id}/sync-price")
# def manual_product_price_sync(product_id: int, db: Session = Depends(get_db)):
#     """Force sync price from AliExpress to Shopify for this imported product."""
#     product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
#     if not product:
#         raise HTTPException(404, "Product not found")
#     if not product.shopify_product_id:
#         raise HTTPException(400, "This product has no Shopify ID attached")
#     success = sync_product_price(product_id, db)
#     return {"message": "Price sync completed" if success else "Price sync failed (no change or error)"}


@app.post("/dashboard/products/{product_id}/sync-price")
def manual_product_price_sync(product_id: int, db: Session = Depends(get_db)):
    """Manual sync: fetch latest AliExpress price, update Shopify, and reset mode to auto."""
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "This product has no Shopify ID attached")
    if not product.track_price:
        raise HTTPException(400, "Price tracking is disabled for this product")

    try:
        # 1. Fetch fresh AliExpress data
        latest_data = get_product(product.aliexpress_id, db)
        new_skus = latest_data.get("skus", [])
        if not new_skus:
            raise HTTPException(500, "No SKUs found in AliExpress product")

        # 2. Update Shopify variants WITHOUT any increase
        success = update_shopify_product_prices_with_skus(product.shopify_product_id, new_skus)
        if not success:
            raise HTTPException(502, "Shopify variant price update failed")

        # 3. Update local original price
        original_price = latest_data.get("sale_price") or latest_data.get("original_price")
        if original_price:
            product.original_price = original_price

        # 4. Reset price mode to auto and clear increase
        product.price_mode = "auto"
        product.price_increase = 0.0
        product.custom_price = None   # remove any manual override
        db.commit()

        return {
            "message": "Price synced from AliExpress and mode reset to Auto",
            "price_mode": "auto"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Sync failed: {str(e)}")


@app.post("/mappings/{mapping_id}/update-price")
def update_mapping_price(mapping_id: int, payload: dict, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    
    new_price = payload.get("price")
    if not new_price:
        raise HTTPException(400, "Price is required")
    try:
        new_price = float(new_price)
    except ValueError:
        raise HTTPException(400, "Invalid price format")
    
    # Set mode to manual
    mapping.price_mode = "manual"
    mapping.price_increase = 0.0
    db.commit()
    
    from .shopify import update_shopify_product_price
    success = update_shopify_product_price(mapping.shopify_product_id, new_price)
    if not success:
        raise HTTPException(502, "Failed to update Shopify product price")
    
    return {"message": f"Price updated to {new_price} (manual mode)", "product_id": mapping.shopify_product_id, "price_mode": "manual"}


# @app.post("/mappings/{mapping_id}/increase-price")
# def increase_mapping_price(mapping_id: int, payload: dict, db: Session = Depends(get_db)):
#     mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
#     if not mapping:
#         raise HTTPException(404, "Mapping not found")
    
#     increase_by = payload.get("increase_by")
#     if increase_by is None:
#         raise HTTPException(400, "increase_by amount is required")
#     try:
#         increase_by = float(increase_by)
#     except ValueError:
#         raise HTTPException(400, "Invalid amount format")
    
#     from .shopify import increase_shopify_product_price
#     success = increase_shopify_product_price(mapping.shopify_product_id, increase_by)
#     if not success:
#         raise HTTPException(502, "Failed to increase Shopify product prices")
    
#     return {"message": f"Increased all variants by ${increase_by:.2f}", "product_id": mapping.shopify_product_id}


@app.post("/mappings/{mapping_id}/increase-price")
def increase_mapping_price(mapping_id: int, payload: dict, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    
    increase_by = payload.get("increase_by")
    if increase_by is None:
        raise HTTPException(400, "increase_by amount is required")
    try:
        increase_by = float(increase_by)
    except ValueError:
        raise HTTPException(400, "Invalid amount format")
    
    mapping.price_mode = "increase"
    mapping.price_increase = increase_by
    db.commit()
    
    from .shopify import increase_shopify_product_price
    success = increase_shopify_product_price(mapping.shopify_product_id, increase_by)
    if not success:
        raise HTTPException(502, "Failed to increase Shopify product prices")
    
    return {"message": f"Increased all variants by ${increase_by:.2f} (increase mode)", "product_id": mapping.shopify_product_id, "price_mode": "increase", "price_increase": increase_by}


# @app.post("/dashboard/products/{product_id}/increase-price")
# def increase_imported_product_price(
#     product_id: int,
#     payload: dict,
#     db: Session = Depends(get_db)
# ):
#     product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
#     if not product:
#         raise HTTPException(404, "Product not found")
#     if not product.shopify_product_id:
#         raise HTTPException(400, "Product has no Shopify ID linked")

#     increase_by = payload.get("increase_by")
#     if increase_by is None:
#         raise HTTPException(400, "increase_by amount is required")
#     try:
#         increase_by = float(increase_by)
#     except ValueError:
#         raise HTTPException(400, "Invalid amount")

#     # 1. Update the stored price increase in DB
#     product.price_increase = increase_by
#     db.commit()

#     # 2. Directly update Shopify variants (same logic as mapping increase)
#     from .shopify import _base, _h
#     import requests

#     try:
#         # Fetch current product variants
#         shop_url = f"{_base()}/products/{product.shopify_product_id}.json"
#         resp = requests.get(shop_url, params={"fields": "id,variants"}, headers=_h(), timeout=15)
#         if resp.status_code != 200:
#             raise HTTPException(502, f"Failed to fetch product: {resp.text}")

#         product_data = resp.json().get("product", {})
#         variants = product_data.get("variants", [])
#         if not variants:
#             raise HTTPException(400, "No variants found in Shopify product")

#         # Calculate new prices
#         updated_variants = []
#         for variant in variants:
#             try:
#                 current_price = float(variant["price"])
#             except (ValueError, TypeError):
#                 current_price = 0.0
#             new_price = current_price + increase_by
#             updated_variants.append({
#                 "id": variant["id"],
#                 "price": f"{new_price:.2f}"
#             })

#         # Send update to Shopify
#         update_payload = {"product": {"variants": updated_variants}}
#         update_resp = requests.put(
#             f"{_base()}/products/{product.shopify_product_id}.json",
#             json=update_payload,
#             headers=_h(),
#             timeout=30
#         )
#         if update_resp.status_code != 200:
#             raise HTTPException(502, f"Shopify update failed: {update_resp.text}")

#         # Update local custom_price for UI display
#         first_new_price = updated_variants[0]["price"] if updated_variants else None
#         if first_new_price:
#             product.custom_price = first_new_price
#             db.commit()

#         return {
#             "message": f"Increased all variants by ${increase_by:.2f}",
#             "price_increase": increase_by,
#             "updated_variants": len(updated_variants)
#         }
#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(500, f"Error updating Shopify: {str(e)}")



@app.post("/dashboard/products/{product_id}/increase-price")
def increase_imported_product_price(
    product_id: int,
    payload: dict,
    db: Session = Depends(get_db)
):
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "Product has no Shopify ID linked")

    increase_by = payload.get("increase_by")
    if increase_by is None:
        raise HTTPException(400, "increase_by amount is required")
    try:
        increase_by = float(increase_by)
    except ValueError:
        raise HTTPException(400, "Invalid amount")

    # Set mode and amount
    product.price_mode = "increase"
    product.price_increase = increase_by
    product.custom_price = None   # ensure manual override is cleared
    db.commit()

    # Directly update Shopify variants
    from .shopify import _base, _h
    import requests

    try:
        shop_url = f"{_base()}/products/{product.shopify_product_id}.json"
        resp = requests.get(shop_url, params={"fields": "id,variants"}, headers=_h(), timeout=15)
        if resp.status_code != 200:
            raise HTTPException(502, f"Failed to fetch product: {resp.text}")

        product_data = resp.json().get("product", {})
        variants = product_data.get("variants", [])
        if not variants:
            raise HTTPException(400, "No variants found in Shopify product")

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

        update_payload = {"product": {"variants": updated_variants}}
        update_resp = requests.put(
            f"{_base()}/products/{product.shopify_product_id}.json",
            json=update_payload,
            headers=_h(),
            timeout=30
        )
        if update_resp.status_code != 200:
            raise HTTPException(502, f"Shopify update failed: {update_resp.text}")

        # Optionally update custom_price for UI consistency
        first_new_price = updated_variants[0]["price"] if updated_variants else None
        if first_new_price:
            product.custom_price = first_new_price
            db.commit()

        return {
            "message": f"Increased all variants by ${increase_by:.2f}",
            "price_mode": "increase",
            "price_increase": increase_by,
            "updated_variants": len(updated_variants)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error updating Shopify: {str(e)}")



@app.post("/admin/backfill-sku-metafields")
def backfill_sku_metafields(db: Session = Depends(get_db)):
    from .shopify import store_aliexpress_sku_ids
    products = db.query(ImportedProduct).filter(ImportedProduct.shopify_product_id.isnot(None)).all()
    count = 0
    for prod in products:
        if not prod.skus:
            print(f"No SKU data for {prod.aliexpress_id}, skipping")
            continue
        try:
            store_aliexpress_sku_ids(prod.shopify_product_id, prod.skus)
            count += 1
            print(f"Backfilled SKU metafields for {prod.aliexpress_id}")
        except Exception as e:
            print(f"Error for {prod.aliexpress_id}: {e}")
    return {"backfilled": count}


# In app/main.py

def update_shipping_inventory_for_all():
    """Update shipping info and total stock for all linked products."""
    db = SessionLocal()
    products = db.query(ImportedProduct).filter(
        ImportedProduct.shopify_product_id.isnot(None)
    ).all()

    for product in products:
        try:
            # --- 1. Fetch Latest AliExpress Data ---
            # This calls your existing get_product() function.
            latest_product_data = get_product(product.aliexpress_id, db)

            # --- 2. Update Total Stock ---
            # The API might provide a top-level 'total_stock' field.
            new_total_stock = latest_product_data.get('total_stock')
            if new_total_stock is not None:
                product.total_stock = int(new_total_stock) if isinstance(new_total_stock, (int, float)) else None

            # --- 3. Update Shipping Info ---
            # Fetch the latest shipping data.
            shipping_info = get_shipping_info(product.aliexpress_id, 'US', db)
            if shipping_info:
                product.shipping_cost = shipping_info.get('shipping_cost')
                product.shipping_method = shipping_info.get('method')

            product.last_shipment_fetch = func.now()
            db.commit()
            print(f"[Data Update] Updated product {product.aliexpress_id}")

        except Exception as e:
            print(f"[Data Update] Error updating product {product.aliexpress_id}: {e}")
            db.rollback()
    db.close()




# ─────────────────────────────────────────────
# REPLACEMENT for the /dashboard/products/{product_id}/details endpoint
# in main.py — replace the existing @app.get("/dashboard/products/{product_id}/details")
# ─────────────────────────────────────────────

@app.get("/dashboard/products/{product_id}/details")
def get_product_details(product_id: int, db: Session = Depends(get_db)):
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")

    # Always fetch fresh data from AliExpress so the modal shows live info.
    # Shipping and inventory are embedded in the product response — no extra call.
    live_data = None
    fetch_error = None
    try:
        from .aliexpress import get_product as ali_get_product
        live_data = ali_get_product(product.aliexpress_id, db)
    except Exception as e:
        fetch_error = str(e)

    if live_data:
        # Persist shipping to DB while we have fresh data
        shipping = live_data.get("shipping_info", {})
        if shipping.get("cost") and shipping["cost"] != "Calculated at checkout":
            product.shipping_cost = shipping["cost"]
        if shipping.get("method"):
            product.shipping_method = shipping["method"]

        # Persist stock if we actually got a number
        if live_data.get("total_stock") is not None:
            product.total_stock = live_data["total_stock"]

        from sqlalchemy.sql import func as sqlfunc
        product.last_shipment_fetch = sqlfunc.now()
        db.commit()

    # Build response — prefer live data, fall back to DB cache
    def _shipping_cost():
        if live_data:
            return live_data.get("shipping_info", {}).get("cost") or product.shipping_cost or "Calculated at checkout"
        return product.shipping_cost or "Calculated at checkout"

    def _shipping_method():
        if live_data:
            return live_data.get("shipping_info", {}).get("method") or product.shipping_method or "Standard Shipping"
        return product.shipping_method or "Standard Shipping"

    def _shipping_days():
        if live_data:
            return live_data.get("shipping_info", {}).get("days") or ""
        return ""

    return {
        "aliexpress_id":      product.aliexpress_id,
        "last_shipment_fetch": product.last_shipment_fetch.isoformat() if product.last_shipment_fetch else None,

        # Shipping
        "shipping_cost":   _shipping_cost(),
        "shipping_method": _shipping_method(),
        "shipping_days":   _shipping_days(),

        # Inventory
        "total_stock":     live_data.get("total_stock")     if live_data else product.total_stock,
        "stock_available": live_data.get("stock_available") if live_data else None,
        "stock_source":    live_data.get("stock_source")    if live_data else "cached",
        "stock_note":      live_data.get("stock_note")      if live_data else (
            "Using cached data — live fetch failed." if fetch_error else None
        ),
        "sku_inventory":   live_data.get("sku_inventory")   if live_data else [],

        # Context
        "orders":          live_data.get("orders")          if live_data else None,
        "fetch_error":     fetch_error,
    }


# ---------- BULK INCREASE FOR IMPORTED PRODUCTS ----------
@app.post("/dashboard/products/bulk-increase")
def bulk_increase_imported_products(
    payload: dict,
    db: Session = Depends(get_db)
):
    """
    payload: {
        "product_ids": [1,2,3],
        "increase_by": 5.00
    }
    """
    product_ids = payload.get("product_ids", [])
    increase_by = payload.get("increase_by")
    if not product_ids or increase_by is None:
        raise HTTPException(400, "Missing product_ids or increase_by")
    try:
        increase_by = float(increase_by)
    except ValueError:
        raise HTTPException(400, "Invalid increase amount")

    # Fetch all selected products that have a Shopify ID
    products = db.query(ImportedProduct).filter(
        ImportedProduct.id.in_(product_ids),
        ImportedProduct.shopify_product_id.isnot(None)
    ).all()

    if not products:
        raise HTTPException(404, "No valid products found")

    results = []
    for prod in products:
        # Update stored price_increase for future syncs
        prod.price_increase = increase_by
        # Immediately apply increase to Shopify
        success = increase_shopify_product_price(prod.shopify_product_id, increase_by)
        results.append({
            "id": prod.id,
            "aliexpress_id": prod.aliexpress_id,
            "success": success
        })
    db.commit()
    return {"message": f"Bulk increase applied to {len(results)} products", "results": results}


# ---------- BULK INCREASE FOR MAPPED PRODUCTS ----------


@app.post("/mappings/bulk-increase")
def bulk_increase_mappings(
    payload: dict,
    db: Session = Depends(get_db)
):
    """
    Bulk increase price for multiple mappings.
    payload: {
        "mapping_ids": [1,2,3],
        "increase_by": 5.00
    }
    """
    mapping_ids = payload.get("mapping_ids", [])
    increase_by = payload.get("increase_by")
    if not mapping_ids or increase_by is None:
        raise HTTPException(400, "Missing mapping_ids or increase_by")
    try:
        increase_by = float(increase_by)
    except ValueError:
        raise HTTPException(400, "Invalid increase amount")

    mappings = db.query(ProductMapping).filter(
        ProductMapping.id.in_(mapping_ids),
        ProductMapping.track_price == True
    ).all()

    if not mappings:
        raise HTTPException(404, "No active mappings found")

    from .shopify import increase_shopify_product_price

    results = []
    for m in mappings:
        try:
            success = increase_shopify_product_price(m.shopify_product_id, increase_by)
            results.append({
                "id": m.id,
                "aliexpress_id": m.aliexpress_id,
                "shopify_product_id": m.shopify_product_id,
                "success": success
            })
        except Exception as e:
            results.append({
                "id": m.id,
                "aliexpress_id": m.aliexpress_id,
                "success": False,
                "error": str(e)
            })

    return {
        "message": f"Bulk increase processed for {len(results)} mapping(s)",
        "results": results
    }


def get_product_display_price(product: ImportedProduct, live_aliexpress_price: float = None) -> float:
    if product.custom_price:
        return float(product.custom_price)
    if product.price_mode == 'increase' and product.price_increase:
        base = live_aliexpress_price or float(product.original_price or 0)
        return base + product.price_increase
    # auto or no increase
    return live_aliexpress_price or float(product.original_price or 0)


@app.post("/dashboard/products/{product_id}/reset-price-mode")
def reset_product_price_mode(product_id: int, db: Session = Depends(get_db)):
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    product.price_mode = "auto"
    product.price_increase = 0.0
    product.custom_price = None
    db.commit()
    # Trigger a sync to fetch latest AliExpress price
    sync_product_price(product_id, db)
    return {"message": "Price mode reset to auto", "price_mode": "auto"}


@app.post("/mappings/{mapping_id}/reset-mode")
def reset_mapping_mode(mapping_id: int, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    mapping.price_mode = "auto"
    mapping.price_increase = 0.0
    db.commit()
    # Optionally sync immediately
    sync_mapped_product_price(mapping.aliexpress_id, db)
    return {"message": "Mapping reset to auto sync mode", "price_mode": "auto"}


# Add this temporary debug endpoint to main.py to see the raw AliExpress response
# GET /debug/aliexpress-raw/{aliexpress_id}
# Call it once, check what fields are actually in the response, then remove it.

@app.get("/debug/aliexpress-raw/{aliexpress_id}")
def debug_aliexpress_raw(aliexpress_id: str, db: Session = Depends(get_db)):
    """
    Returns the FULL raw AliExpress API response so you can see exactly
    what fields are available for shipping and inventory.
    Remove this endpoint after debugging.
    """
    from .auth import get_latest_token
    from .aliexpress import _call, _sign, _check_error
    import time, urllib.parse, hashlib, http.client, json
    from .config import get_settings
    settings = get_settings()

    token = get_latest_token(db)

    sys_params = {
        "app_key":      settings.ALIEXPRESS_APP_KEY,
        "method":       "aliexpress.ds.product.get",
        "timestamp":    str(int(time.time() * 1000)),
        "sign_method":  "md5",
        "v":            "2.0",
        "access_token": token.access_token,
    }
    app_params = {
        "product_id":      aliexpress_id,
        "local":           "en_US",
        "ship_to_country": "US",
        "target_currency": "USD",
    }
    sign_str = settings.ALIEXPRESS_APP_SECRET + "".join(
        f"{k}{({**sys_params, **app_params})[k]}" for k in sorted({**sys_params, **app_params}.keys())
    ) + settings.ALIEXPRESS_APP_SECRET
    sys_params["sign"] = hashlib.md5(sign_str.encode()).hexdigest().upper()

    url  = "/sync?" + urllib.parse.urlencode(sys_params)
    body = urllib.parse.urlencode(app_params)
    conn = http.client.HTTPSConnection("api-sg.aliexpress.com", timeout=30)
    conn.request("POST", url, body=body, headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    resp   = conn.getresponse()
    result = json.loads(resp.read().decode("utf-8"))
    conn.close()

    # Extract just the parts relevant to shipping and inventory
    r = result.get("aliexpress_ds_product_get_response", {}).get("result", {})
    base = r.get("ae_item_base_info_dto", {})
    sku_list = r.get("ae_item_sku_info_dtos", {}).get("ae_item_sku_info_d_t_o", [])

    return {
        "shipping_related_fields": {
            "ae_item_properties":  r.get("ae_item_properties"),
            "logistics_info_dto":  r.get("logistics_info_dto"),
            "freight_template":    r.get("ae_item_properties", {}).get("freight_template"),
            "delivery_days":       base.get("delivery_days"),
        },
        "inventory_related_fields": {
            "total_available_stock":          base.get("total_available_stock"),
            "product_inventory_quantity":     base.get("product_inventory_quantity"),
            "total_stock":                    base.get("total_stock"),
            "lastest_volume":                 base.get("lastest_volume"),
            "volume":                         base.get("volume"),
            "sku_stocks": [
                {
                    "sku_id":         s.get("sku_id"),
                    "sku_attr":       s.get("sku_attr"),
                    "ipm_sku_stock":  s.get("ipm_sku_stock"),
                    "sku_stock":      s.get("sku_stock"),          # alternate field name
                    "available":      s.get("available"),          # alternate field name
                    "all_sku_fields": list(s.keys()),              # show ALL field names
                }
                for s in sku_list[:5]  # first 5 SKUs only
            ],
        },
        "all_result_keys":    list(r.keys()),
        "all_base_info_keys": list(base.keys()),
    }

# ─────────────────────────────────────────────
# NEW ENDPOINT — add to main.py
# Returns all Shopify variants for a product with their current prices,
# plus the AliExpress SKU label for each (so the modal can show
# "Red / XL — $19.99" etc.)
# ─────────────────────────────────────────────


@app.get("/dashboard/products/{product_id}/variants")
def get_product_variants(product_id: int, db: Session = Depends(get_db)):
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        return {"variants": [], "message": "No Shopify product linked"}

    from .shopify import _base, _h
    res = requests.get(
        f"{_base()}/products/{product.shopify_product_id}.json",
        params={"fields": "id,title,variants,options"},
        headers=_h(), timeout=15,
    )
    if res.status_code != 200:
        raise HTTPException(502, res.text)

    shopify_product = res.json().get("product", {})
    variants = shopify_product.get("variants", [])

    result = []
    for v in variants:
        # Build a readable label from option1/2/3
        label_parts = [v.get(f"option{i}") for i in (1, 2, 3) if v.get(f"option{i}") and v.get(f"option{i}") != "Default Title"]
        label = " / ".join(label_parts) if label_parts else "Default"

        result.append({
            "variant_id": v["id"],
            "label": label,
            "sku": v.get("sku"),
            "price": v.get("price"),
            "compare_at_price": v.get("compare_at_price"),
            "inventory_quantity": v.get("inventory_quantity"),
        })

    return {
        "shopify_product_id": product.shopify_product_id,
        "title": shopify_product.get("title"),
        "variants": result,
    }


# ─────────────────────────────────────────────
# NEW ENDPOINT — bulk update variant prices individually
# Use this when the user edits each variant's price separately in the modal
# ─────────────────────────────────────────────

@app.post("/dashboard/products/{product_id}/update-variant-prices")
def update_variant_prices(product_id: int, payload: dict, db: Session = Depends(get_db)):
    """
    payload: {
        "variants": [
            {"variant_id": 123456, "price": "19.99"},
            {"variant_id": 123457, "price": "21.99"}
        ]
    }
    """
    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "Product has no Shopify ID linked")

    variants_payload = payload.get("variants", [])
    if not variants_payload:
        raise HTTPException(400, "No variants provided")

    from .shopify import _base, _h

    updated_variants = []
    for v in variants_payload:
        vid = v.get("variant_id")
        price = v.get("price")
        if vid is None or price is None:
            continue
        try:
            updated_variants.append({"id": int(vid), "price": str(float(price))})
        except (ValueError, TypeError):
            raise HTTPException(400, f"Invalid price for variant {vid}")

    if not updated_variants:
        raise HTTPException(400, "No valid variant updates provided")

    res = requests.put(
        f"{_base()}/products/{product.shopify_product_id}.json",
        json={"product": {"variants": updated_variants}},
        headers=_h(), timeout=30,
    )
    if res.status_code != 200:
        raise HTTPException(502, f"Shopify update failed: {res.text}")

    # Switch to manual mode since the user explicitly set prices per-variant
    product.price_mode = "manual"
    product.price_increase = 0.0
    # Update custom_price to reflect the first variant's price for table display
    if updated_variants:
        product.custom_price = updated_variants[0]["price"]
    db.commit()

    return {
        "message": f"Updated {len(updated_variants)} variant price(s) (manual mode)",
        "price_mode": "manual",
        "updated": len(updated_variants),
    }