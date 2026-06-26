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
from .models import ImportedProduct, Base, PendingImport
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

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from sqlalchemy.sql import func as sqlfunc
from .database import SessionLocal



settings = get_settings()
Base.metadata.create_all(bind=engine)

scheduler = None

IMPORT_MODE = "all" 

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
    if not product:
        print(f"[Sync][DEBUG] Product id={product_id} not found in DB")
        return False
    if not product.track_price:
        print(f"[Sync][DEBUG] Product {product.aliexpress_id} has track_price=False, skipping")
        return False
    if not product.aliexpress_id:
        print(f"[Sync][DEBUG] Product id={product_id} has no aliexpress_id, skipping")
        return False

    print(f"[Sync][DEBUG] Starting sync for {product.aliexpress_id} (mode={product.price_mode})")

    if product.price_mode == 'manual':
        print(f"[Sync] Skipping manual product {product.aliexpress_id}")
        return False

    try:
        latest_data = get_product(product.aliexpress_id, db)
        print(f"[Sync][DEBUG] Fetched AliExpress data for {product.aliexpress_id}, "
              f"got {len(latest_data.get('skus', []))} SKUs")
    except Exception as e:
        print(f"[Sync] Failed to fetch product {product.aliexpress_id}: {e}")
        return False

    new_skus = latest_data.get("skus", [])
    if not new_skus:
        print(f"[Sync] No SKUs for {product.aliexpress_id}")
        return False

    if product.price_mode == 'increase' and product.price_increase != 0.0:
        for sku in new_skus:
            current_price = sku.get("sale_price") or sku.get("price")
            if current_price is not None:
                new_price = float(current_price) + product.price_increase
                sku["sale_price"] = str(new_price)
                sku["price"] = str(new_price)

    shopify_updated = False
    inventory_updated = False
    if product.shopify_product_id:
        print(f"[Sync][DEBUG] Pushing price update to Shopify product {product.shopify_product_id}")
        shopify_updated = update_shopify_product_prices_with_skus(
            product.shopify_product_id, new_skus
        )
        from .shopify import update_shopify_product_inventory_with_skus
        print(f"[Sync][DEBUG] Pushing inventory update to Shopify product {product.shopify_product_id}")
        inventory_updated = update_shopify_product_inventory_with_skus(
            product.shopify_product_id, new_skus
        )
    else:
        print(f"[Sync][DEBUG] Product {product.aliexpress_id} has no shopify_product_id, skipping Shopify push")

    original_price = latest_data.get("sale_price") or latest_data.get("original_price")
    if original_price:
        product.original_price = original_price

    new_total_stock = latest_data.get("total_stock")
    if new_total_stock is not None:
        product.total_stock = new_total_stock

    db.commit()
    print(f"[Sync] Product {product.aliexpress_id} processed (mode={product.price_mode}). "
          f"Shopify price updated: {shopify_updated}, inventory updated: {inventory_updated}")
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


# def sync_all_tracked_products():
#     print(f"[Sync][DEBUG] sync_all_tracked_products() invoked")
#     sync_existing_shopify_products_to_db()
#     db = SessionLocal()
#     try:
#         products = db.query(ImportedProduct).filter(ImportedProduct.track_price == True).all()
#         print(f"[Sync] Starting price sync for {len(products)} products")
#         if not products:
#             print("[Sync][DEBUG] No products with track_price=True found — nothing to sync")
#         for p in products:
#             sync_product_price(p.id, db)
#     except Exception as e:
#         print(f"[Sync] Background task error: {e}")
#     finally:
#         db.close()


def sync_all_tracked_products():
    print(f"[Sync][DEBUG] sync_all_tracked_products() invoked")
    sync_existing_shopify_products_to_db()
    db = SessionLocal()
    try:
        products = db.query(ImportedProduct).filter(ImportedProduct.track_price == True).all()
        print(f"[Sync] Starting price sync for {len(products)} products")
        for p in products:
            sync_product_price(p.id, db)
            # After price sync, check if this product has OOS variants
            # and queue it if not already tracked
            try:
                existing_pending = db.query(PendingImport).filter(
                    PendingImport.aliexpress_id == p.aliexpress_id,
                    PendingImport.status.in_(["pending", "partial"])
                ).first()
                if not existing_pending:
                    raw = get_product(p.aliexpress_id, db)
                    stock_info = classify_skus(raw)
                    if stock_info["out_of_stock"]:
                        upsert_pending(p.aliexpress_id, raw, stock_info, "pending", db)
                        print(f"[Sync] Auto-queued {p.aliexpress_id} — "
                              f"{len(stock_info['out_of_stock'])} OOS variant(s) detected")
            except Exception as e:
                print(f"[Sync] OOS check failed for {p.aliexpress_id}: {e}")
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

# def start_scheduler():
#     global scheduler
#     scheduler = BackgroundScheduler()
#     # Existing job for imported products
#     scheduler.add_job(
#         sync_all_tracked_products,
#         trigger=IntervalTrigger(hours=1),
#         id="price_sync_job",
#         replace_existing=True,
#     )
#     # NEW job for mapped products
#     scheduler.add_job(
#         sync_all_mapped_products_background,
#         trigger=IntervalTrigger(hours=1),
#         id="mapped_price_sync_job",
#         replace_existing=True,
#     )
#     scheduler.start()
#     print("[Scheduler] Started – will sync prices every hour for both imported and mapped products")        


# main.py

def start_scheduler():
    global scheduler
    print("[Scheduler] Starting scheduler...")
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

        scheduler.add_job(
            sync_all_tracked_products,
            trigger=IntervalTrigger(hours=1),
            id="price_sync_job",
            replace_existing=True,
        )
        scheduler.add_job(
            sync_all_mapped_products_background,
            trigger=IntervalTrigger(hours=1),
            id="mapped_price_sync_job",
            replace_existing=True,
        )
        scheduler.add_job(
            process_pending_imports,
            trigger=IntervalTrigger(minutes=30),
            id="pending_import_job",
            replace_existing=True,
        )

        scheduler.start()
        jobs = scheduler.get_jobs()
        print(f"[Scheduler] Started with {len(jobs)} job(s):")
        for j in jobs:
            print(f"  - {j.id} → next run at {j.next_run_time}")
    except Exception as e:
        print(f"[Scheduler] ERROR: {e}")
        import traceback
        traceback.print_exc()
        scheduler = None 

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


#app = FastAPI(title="AliShopify Backend", lifespan=lifespan)
app = FastAPI(title="AliShopify Backend")




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


@asynccontextmanager
async def root_lifespan(root_app: FastAPI):
    start_scheduler()
    sync_existing_shopify_products_to_db()   # initial sync
    yield
    shutdown_scheduler()

root_app = _RootFastAPI(title="AliShopify Backend Root", lifespan=root_lifespan)
#root_app.add_middleware(...)   # CORS
#root_app.mount("/api", app)  
 
#root_app = _RootFastAPI(title="AliShopify Backend Root")
 
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
# def list_products(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(5, ge=1, le=100),
#     db: Session = Depends(get_db)
# ):
#     query = db.query(ImportedProduct)
#     total = query.count()
#     pages = math.ceil(total / page_size) if total > 0 else 1
#     offset = (page - 1) * page_size
#     products = query.order_by(ImportedProduct.imported_at.desc()).offset(offset).limit(page_size).all()

def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=100),
    search: str = Query(None, description="Search by title or AliExpress ID"),
    db: Session = Depends(get_db)
):
    query = db.query(ImportedProduct)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (ImportedProduct.original_title.ilike(search_term)) |
            (ImportedProduct.custom_title.ilike(search_term)) |
            (ImportedProduct.aliexpress_id.ilike(search_term))
        )
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




# @app.post("/import/{aliexpress_id}")
# def import_to_shopify(aliexpress_id: str, db: Session = Depends(get_db)):
#     # 1. Check if this ID is already in product_mappings table
#     mapping_exists = db.query(ProductMapping).filter(ProductMapping.aliexpress_id == aliexpress_id).first()
#     if mapping_exists:
#         raise HTTPException(
#             status_code=409,
#             detail=f"AliExpress ID {aliexpress_id} is already mapped to Shopify product {mapping_exists.shopify_product_id}. "
#                    f"Use the 'Sync Mappings' page to update its price – do not import again."
#         )

#     # 2. Check local DB
#     existing = db.query(ImportedProduct).filter(ImportedProduct.aliexpress_id == aliexpress_id).first()
#     if existing and existing.shopify_product_id:
#         raise HTTPException(409, "Product already imported to Shopify")

#     # 3. Fetch fresh data from AliExpress
#     raw_product = get_product(aliexpress_id, db)

#     # 4. If not in local DB, check if product already exists in Shopify (by title)
#     title = raw_product.get("title")
#     if title and check_product_exists_in_shopify(title):
#         # Find existing Shopify product ID
#         shop = settings.SHOPIFY_STORE.replace(".myshopify.com", "").strip()
#         token = get_shopify_token()
#         try:
#             res = requests.get(
#                 f"https://{shop}.myshopify.com/admin/api/{settings.SHOPIFY_API_VERSION}/products.json",
#                 params={"title": title, "limit": 1, "fields": "id,title,status"},
#                 headers={"X-Shopify-Access-Token": token},
#                 timeout=15
#             )
#             res.raise_for_status()
#             products = res.json().get("products", [])
#             if products:
#                 shopify_id = str(products[0]["id"])
#                 shopify_status = products[0].get("status", "draft")
#                 # Create local record only (no new Shopify product)
#                 if existing:
#                     existing.shopify_product_id = shopify_id
#                     existing.shopify_status = shopify_status
#                     existing.original_title = raw_product.get("title")
#                     existing.original_price = raw_product.get("sale_price") or raw_product.get("original_price")
#                     existing.currency = raw_product.get("currency")
#                     existing.main_image = raw_product.get("main_image")
#                     existing.store_name = raw_product.get("store_name")
#                     existing.avg_rating = raw_product.get("avg_rating")
#                     existing.sku_count = raw_product.get("sku_count")
#                     existing.skus = raw_product.get("skus")
#                     existing.all_images = raw_product.get("all_images")
#                     existing.track_price = True
#                     db.commit()
#                     db.refresh(existing)
#                     product_id = existing.id
#                 else:
#                     new_product = ImportedProduct(
#                         aliexpress_id=aliexpress_id,
#                         original_title=raw_product.get("title"),
#                         original_price=raw_product.get("sale_price") or raw_product.get("original_price"),
#                         currency=raw_product.get("currency"),
#                         main_image=raw_product.get("main_image"),
#                         all_images=raw_product.get("all_images"),
#                         store_name=raw_product.get("store_name"),
#                         avg_rating=raw_product.get("avg_rating"),
#                         review_count=raw_product.get("review_count"),
#                         orders=raw_product.get("orders"),
#                         sku_count=raw_product.get("sku_count"),
#                         skus=raw_product.get("skus"),
#                         shopify_product_id=shopify_id,
#                         shopify_status=shopify_status,
#                         track_price=True,
#                     )
#                     db.add(new_product)
#                     db.commit()
#                     db.refresh(new_product)
#                     product_id = new_product.id
#                 return {
#                     "message": "Product already existed in Shopify – local record created/updated. No duplicate created.",
#                     "product_id": product_id,
#                     "shopify_product": {"id": shopify_id, "title": title, "status": shopify_status}
#                 }
#         except Exception as e:
#             print(f"Error checking Shopify product: {e}")
#             raise HTTPException(409, f"Product '{title}' already exists in Shopify (title match), but could not retrieve its ID. Please contact support.")

#     # 5. If not found in Shopify, create new product
#     shopify_resp = create_shopify_product(raw_product)
#     shopify_product_new = shopify_resp.get("product", {})
#     shopify_id = str(shopify_product_new.get("id"))

#     if existing:
#         existing.shopify_product_id = shopify_id
#         existing.shopify_status = shopify_product_new.get("status", "draft")
#         existing.original_title = raw_product.get("title")
#         existing.original_price = raw_product.get("sale_price") or raw_product.get("original_price")
#         existing.currency = raw_product.get("currency")
#         existing.main_image = raw_product.get("main_image")
#         existing.store_name = raw_product.get("store_name")
#         existing.avg_rating = raw_product.get("avg_rating")
#         existing.review_count = raw_product.get("review_count")
#         existing.orders = raw_product.get("orders")
#         existing.sku_count = raw_product.get("sku_count")
#         existing.skus = raw_product.get("skus")
#         existing.all_images = raw_product.get("all_images")
#         existing.track_price = True
#         db.commit()
#         db.refresh(existing)
#         product_id = existing.id
#     else:
#         new_product = ImportedProduct(
#             aliexpress_id=aliexpress_id,
#             original_title=raw_product.get("title"),
#             original_price=raw_product.get("sale_price") or raw_product.get("original_price"),
#             currency=raw_product.get("currency"),
#             main_image=raw_product.get("main_image"),
#             all_images=raw_product.get("all_images"),
#             store_name=raw_product.get("store_name"),
#             avg_rating=raw_product.get("avg_rating"),
#             review_count=raw_product.get("review_count"),
#             orders=raw_product.get("orders"),
#             sku_count=raw_product.get("sku_count"),
#             skus=raw_product.get("skus"),
#             shopify_product_id=shopify_id,
#             shopify_status=shopify_product_new.get("status", "draft"),
#             track_price=True,
#         )
#         db.add(new_product)
#         db.commit()
#         db.refresh(new_product)
#         product_id = new_product.id

#     return {
#         "message": "Imported successfully",
#         "product_id": product_id,
#         "shopify_product": {
#             "id": shopify_id,
#             "title": shopify_product_new.get("title"),
#             "status": shopify_product_new.get("status"),
#         }
#     }
#     # main.py – add after existing endpoints



# main.py

@app.post("/import/{aliexpress_id}")
def import_to_shopify(aliexpress_id: str, db: Session = Depends(get_db)):

    # 1. Already in pending queue (just return status, don't block)
    existing_pending = db.query(PendingImport).filter(
        PendingImport.aliexpress_id == aliexpress_id,
        PendingImport.status == "pending",
    ).first()

    # 2. Fetch fresh data
    raw_product = get_product(aliexpress_id, db)
    stock_info = classify_skus(raw_product)

    print(f"[Import][DEBUG] {aliexpress_id}: "
          f"in_stock={len(stock_info['in_stock'])} "
          f"out_of_stock={len(stock_info['out_of_stock'])} "
          f"unknown={len(stock_info['unknown'])}")

    # 3. ALWAYS import to Shopify (no longer blocking on OOS)
    try:
        product, created = import_aliexpress_product_to_shopify(raw_product, db)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(500, f"Import failed: {str(e)}")

    shopify_info = {
        "id": product.shopify_product_id,
        "title": product.original_title,
        "status": product.shopify_status,
    }

    oos = stock_info["out_of_stock"]

    # 4. If there are OOS variants, add to pending queue for inventory tracking
    if oos:
        upsert_pending(aliexpress_id, raw_product, stock_info, "pending", db)
        return {
            "status": "imported",
            "product_id": product.id,
            "out_of_stock_skus": oos,
            "shopify_product": shopify_info,
            "message": (
                f"Imported to Shopify successfully. "
                f"{len(oos)} variant(s) are out of stock — "
                f"added to pending queue and will auto-update inventory when stock returns."
            ),
            "queued_for_inventory_sync": True,
        }

    # 5. All variants in stock — clean import
    return {
        "status": "imported",
        "product_id": product.id,
        "out_of_stock_skus": [],
        "shopify_product": shopify_info,
        "message": "Imported successfully" if created else "Linked to existing Shopify product",
        "queued_for_inventory_sync": False,
    }



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

# @app.get("/mappings/list")
# def list_mappings(db: Session = Depends(get_db)):
#     mappings = db.query(ProductMapping).all()
#     return [{
#         "id": m.id,
#         "aliexpress_id": m.aliexpress_id,
#         "shopify_product_id": m.shopify_product_id,
#         "title": m.shopify_product_title,
#         "track_price": m.track_price,
#         "price_mode": m.price_mode,
#         "price_increase": m.price_increase
#     } for m in mappings]


@app.get("/mappings/list")
def list_mappings(
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db)
):
    total = db.query(ProductMapping).count()
    pages = (total + page_size - 1) // page_size if total > 0 else 1
    offset = (page - 1) * page_size

    mappings = db.query(ProductMapping)\
        .order_by(ProductMapping.created_at.desc())\
        .offset(offset)\
        .limit(page_size)\
        .all()

    return {
        "mappings": [{
            "id": m.id,
            "aliexpress_id": m.aliexpress_id,
            "shopify_product_id": m.shopify_product_id,
            "title": m.shopify_product_title,
            "track_price": m.track_price,
            "price_mode": m.price_mode,
            "price_increase": m.price_increase
        } for m in mappings],
        "total": total,
        "page": page,
        "pages": pages
    }


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

#     # Allow sync regardless of current mode – we will reset to auto
#     get_latest_token(db)

#     try:
#         raw = get_product(aliexpress_id, db)
#     except Exception as e:
#         raise HTTPException(500, f"Failed to fetch from AliExpress: {str(e)}")

#     aliexpress_skus = raw.get("skus", [])
#     if not aliexpress_skus:
#         raise HTTPException(500, "No SKUs found in AliExpress product")

#     # Update Shopify with base prices – no increase applied
#     from .shopify import update_shopify_product_prices_with_skus
#     result = update_shopify_product_prices_with_skus(mapping.shopify_product_id, aliexpress_skus)

#     if result == "failed":
#         raise HTTPException(502, "Failed to update Shopify variant prices")

#     # Reset mode to auto and clear increase
#     mapping.price_mode = "auto"
#     mapping.price_increase = 0.0
#     db.commit()

#     message = "Price already up to date" if result == "unchanged" else "Variant prices updated to AliExpress base"
#     return {
#         "message": message,
#         "product_id": mapping.shopify_product_id,
#         "price_mode": "auto",
#         "price_increase": 0.0,
#     }


@app.post("/mappings/sync-price")
def sync_mapped_product_price(aliexpress_id: str, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.aliexpress_id == aliexpress_id).first()
    if not mapping or not mapping.track_price:
        raise HTTPException(404, "Mapping not found or price tracking disabled")

    get_latest_token(db)

    try:
        raw = get_product(aliexpress_id, db)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch from AliExpress: {str(e)}")

    aliexpress_skus = raw.get("skus", [])
    if not aliexpress_skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    from .shopify import update_shopify_product_prices_with_skus, update_shopify_product_inventory_with_skus

    # 1. Update prices
    price_result = update_shopify_product_prices_with_skus(mapping.shopify_product_id, aliexpress_skus)
    if price_result == "failed":
        raise HTTPException(502, "Failed to update Shopify variant prices")

    # 2. Update inventory
    inventory_updated = update_shopify_product_inventory_with_skus(mapping.shopify_product_id, aliexpress_skus)

    # Reset mode to auto and clear increase
    mapping.price_mode = "auto"
    mapping.price_increase = 0.0
    db.commit()

    price_msg = "Price already up to date" if price_result == "unchanged" else "Variant prices updated to AliExpress base"
    inv_msg = "Inventory updated" if inventory_updated else "Inventory unchanged or not available"
    return {
        "message": f"{price_msg} · {inv_msg}",
        "product_id": mapping.shopify_product_id,
        "price_mode": "auto",
        "price_increase": 0.0,
        "inventory_updated": inventory_updated,
    }





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
    from .shopify import update_shopify_product_inventory_with_skus
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
                update_shopify_product_inventory_with_skus(m.shopify_product_id, skus)  # NEW
                print(f"[Hourly] Updated price+inventory for {m.aliexpress_id} (mode={m.price_mode})")
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

#     # Accumulate the markup on top of whatever has already been applied
#     previous_increase = mapping.price_increase or 0.0
#     total_increase = previous_increase + increase_by
#     mapping.price_mode = "increase"
#     mapping.price_increase = total_increase

#     # Fetch the current AliExpress base price (NOT the current Shopify price)
#     # so the new Shopify price always = AliExpress base + total accumulated markup,
#     # regardless of how many times this has been clicked before.
#     try:
#         raw = get_product(mapping.aliexpress_id, db)
#         skus = raw.get("skus", [])
#     except Exception as e:
#         raise HTTPException(500, f"Failed to fetch AliExpress base price: {e}")

#     if not skus:
#         raise HTTPException(500, "No SKUs found in AliExpress product")

#     # Apply base + total_increase to each SKU, then push the full set to Shopify
#     for sku in skus:
#         base_price = sku.get("sale_price") or sku.get("price")
#         if base_price is not None:
#             new_price = float(base_price) + total_increase
#             sku["sale_price"] = str(new_price)
#             sku["price"] = str(new_price)

#     from .shopify import update_shopify_product_prices_with_skus
#     success = update_shopify_product_prices_with_skus(mapping.shopify_product_id, skus)
#     if not success:
#         raise HTTPException(502, "Failed to update Shopify variant prices")

#     db.commit()

#     return {
#         "message": f"Increased by ${increase_by:.2f} (total markup now ${total_increase:.2f} above AliExpress base)",
#         "product_id": mapping.shopify_product_id,
#         "price_mode": "increase",
#         "price_increase": total_increase,
#     }







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

    # Accumulate the markup on top of whatever has already been applied
    previous_increase = mapping.price_increase or 0.0
    total_increase = previous_increase + increase_by
    mapping.price_mode = "increase"
    mapping.price_increase = total_increase

    # Fetch the current AliExpress base price (NOT the current Shopify price)
    # so the new Shopify price always = AliExpress base + total accumulated markup,
    # regardless of how many times this has been clicked before.
    try:
        raw = get_product(mapping.aliexpress_id, db)
        skus = raw.get("skus", [])
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch AliExpress base price: {e}")

    if not skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    # Apply base + total_increase to each SKU, then push the full set to Shopify
    for sku in skus:
        base_price = sku.get("sale_price") or sku.get("price")
        if base_price is not None:
            new_price = float(base_price) + total_increase
            sku["sale_price"] = str(new_price)
            sku["price"] = str(new_price)

    from .shopify import update_shopify_product_prices_with_skus
    success = update_shopify_product_prices_with_skus(mapping.shopify_product_id, skus)
    if not success:
        raise HTTPException(502, "Failed to update Shopify variant prices")

    db.commit()

    return {
        "message": f"Increased by ${increase_by:.2f} (total markup now ${total_increase:.2f} above AliExpress base)",
        "product_id": mapping.shopify_product_id,
        "price_mode": "increase",
        "price_increase": total_increase,
    }




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

#     # Set mode and amount
#     product.price_mode = "increase"
#     product.price_increase = increase_by
#     product.custom_price = None   # ensure manual override is cleared
#     db.commit()

#     # Directly update Shopify variants
#     from .shopify import _base, _h
#     import requests

#     try:
#         shop_url = f"{_base()}/products/{product.shopify_product_id}.json"
#         resp = requests.get(shop_url, params={"fields": "id,variants"}, headers=_h(), timeout=15)
#         if resp.status_code != 200:
#             raise HTTPException(502, f"Failed to fetch product: {resp.text}")

#         product_data = resp.json().get("product", {})
#         variants = product_data.get("variants", [])
#         if not variants:
#             raise HTTPException(400, "No variants found in Shopify product")

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

#         update_payload = {"product": {"variants": updated_variants}}
#         update_resp = requests.put(
#             f"{_base()}/products/{product.shopify_product_id}.json",
#             json=update_payload,
#             headers=_h(),
#             timeout=30
#         )
#         if update_resp.status_code != 200:
#             raise HTTPException(502, f"Shopify update failed: {update_resp.text}")

#         # Optionally update custom_price for UI consistency
#         first_new_price = updated_variants[0]["price"] if updated_variants else None
#         if first_new_price:
#             product.custom_price = first_new_price
#             db.commit()

#         return {
#             "message": f"Increased all variants by ${increase_by:.2f}",
#             "price_mode": "increase",
#             "price_increase": increase_by,
#             "updated_variants": len(updated_variants)
#         }
#     except HTTPException:
#         raise
#     except Exception as e:
#         raise HTTPException(500, f"Error updating Shopify: {str(e)}")



# @app.post("/dashboard/products/{product_id}/increase-price")
# def increase_imported_product_price(product_id: int, payload: dict, db: Session = Depends(get_db)):
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

#     previous_increase = product.price_increase or 0.0
#     total_increase = previous_increase + increase_by
#     product.price_mode = "increase"
#     product.price_increase = total_increase
#     product.custom_price = None

#     try:
#         latest_data = get_product(product.aliexpress_id, db)
#         skus = latest_data.get("skus", [])
#     except Exception as e:
#         raise HTTPException(500, f"Failed to fetch AliExpress base price: {e}")

#     if not skus:
#         raise HTTPException(500, "No SKUs found in AliExpress product")

#     for sku in skus:
#         base_price = sku.get("sale_price") or sku.get("price")
#         if base_price is not None:
#             new_price = float(base_price) + total_increase
#             sku["sale_price"] = str(new_price)
#             sku["price"] = str(new_price)

#     success = update_shopify_product_prices_with_skus(product.shopify_product_id, skus)
#     if not success:
#         raise HTTPException(502, "Failed to update Shopify variant prices")

#     original_price = latest_data.get("sale_price") or latest_data.get("original_price")
#     if original_price:
#         product.original_price = original_price

#     db.commit()

#     return {
#         "message": f"Increased by ${increase_by:.2f} (total markup now ${total_increase:.2f} above AliExpress base)",
#         "price_mode": "increase",
#         "price_increase": total_increase,
#         "updated_variants": len(skus),
#     }



@app.post("/dashboard/products/{product_id}/increase-price")
def increase_imported_product_price(product_id: int, payload: dict, db: Session = Depends(get_db)):
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

    previous_increase = product.price_increase or 0.0
    total_increase = previous_increase + increase_by
    product.price_mode = "increase"
    product.price_increase = total_increase
    product.custom_price = None

    try:
        latest_data = get_product(product.aliexpress_id, db)
        skus = latest_data.get("skus", [])
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch AliExpress base price: {e}")

    if not skus:
        raise HTTPException(500, "No SKUs found in AliExpress product")

    for sku in skus:
        base_price = sku.get("sale_price") or sku.get("price")
        if base_price is not None:
            new_price = float(base_price) + total_increase
            sku["sale_price"] = str(new_price)
            sku["price"] = str(new_price)

    success = update_shopify_product_prices_with_skus(product.shopify_product_id, skus)
    if not success:
        raise HTTPException(502, "Failed to update Shopify variant prices")

    original_price = latest_data.get("sale_price") or latest_data.get("original_price")
    if original_price:
        product.original_price = original_price

    db.commit()

    return {
        "message": f"Increased by ${increase_by:.2f} (total markup now ${total_increase:.2f} above AliExpress base)",
        "price_mode": "increase",
        "price_increase": total_increase,
        "updated_variants": len(skus),
    }


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


# @app.post("/mappings/{mapping_id}/reset-mode")
# def reset_mapping_mode(mapping_id: int, db: Session = Depends(get_db)):
#     mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
#     if not mapping:
#         raise HTTPException(404, "Mapping not found")
#     mapping.price_mode = "auto"
#     mapping.price_increase = 0.0
#     db.commit()
#     # Optionally sync immediately
#     sync_mapped_product_price(mapping.aliexpress_id, db)
#     return {"message": "Mapping reset to auto sync mode", "price_mode": "auto"}

@app.post("/mappings/{mapping_id}/reset-mode")
def reset_mapping_mode(mapping_id: int, db: Session = Depends(get_db)):
    mapping = db.query(ProductMapping).filter(ProductMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    mapping.price_mode = "auto"
    mapping.price_increase = 0.0
    db.commit()

    # Trigger an immediate price sync now that mode is auto again
    try:
        get_latest_token(db)
        raw = get_product(mapping.aliexpress_id, db)
        skus = raw.get("skus", [])
        if skus:
            update_shopify_product_prices_with_skus(mapping.shopify_product_id, skus)
    except Exception as e:
        print(f"[ResetMode] Immediate sync after reset failed (non-fatal): {e}")

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



def _job_listener(event):
    if event.exception:
        print(f"[Scheduler][ERROR] Job '{event.job_id}' failed: {event.exception}")
    else:
        print(f"[Scheduler][OK] Job '{event.job_id}' completed successfully")




@app.post("/debug/run-sync-now")
def debug_run_sync_now():
    """Manually trigger both sync jobs immediately, with verbose output."""
    print("\n" + "="*60)
    print("[DEBUG] Manual sync triggered")
    print("="*60)
    try:
        print("[DEBUG] Running sync_all_tracked_products()...")
        sync_all_tracked_products()
        print("[DEBUG] sync_all_tracked_products() finished OK")
    except Exception as e:
        print(f"[DEBUG] sync_all_tracked_products() RAISED: {e}")

    try:
        print("[DEBUG] Running sync_all_mapped_products_background()...")
        sync_all_mapped_products_background()
        print("[DEBUG] sync_all_mapped_products_background() finished OK")
    except Exception as e:
        print(f"[DEBUG] sync_all_mapped_products_background() RAISED: {e}")

    print("="*60 + "\n")
    return {"message": "Manual sync triggered — check terminal output"}

# main.py

# def is_product_in_stock(product_data: dict) -> bool:
#     """
#     Determine if a product has any available stock.
#     Uses total_stock and stock_available flags from the API.
#     """
#     total_stock = product_data.get("total_stock")
#     stock_available = product_data.get("stock_available")

#     # If total_stock is an integer > 0 → in stock
#     if total_stock is not None and isinstance(total_stock, (int, float)) and total_stock > 0:
#         return True

#     # If stock_available is explicitly True → in stock
#     if stock_available is True:
#         return True

#     # If stock_available is False → out of stock
#     if stock_available is False:
#         return False

#     # If total_stock is 0 or None, and stock_available is None, we cannot be sure.
#     # We'll assume out of stock to be safe, but you could allow import by default.
#     # Let's default to False (don't import) to prevent selling unavailable items.
#     return False

# AFTER (corrected)
def is_product_in_stock(product_data: dict) -> bool:
    result = classify_skus(product_data)

    # Remove the no_info shortcut
    if IMPORT_MODE == "all":
        # Require ALL variants to have known stock > 0
        return result["all_in_stock"]
    else:
        return result["any_in_stock"]



# main.py

def import_aliexpress_product_to_shopify(raw_product: dict, db: Session) -> tuple[ImportedProduct, bool]:
    """
    Creates Shopify product (if not already exists) and stores local record.
    Returns (ImportedProduct, created) where created is True if a new Shopify product was made.
    Raises HTTPException on errors.
    """
    aliexpress_id = raw_product.get("product_id")
    if not aliexpress_id:
        raise HTTPException(400, "Missing product_id in AliExpress data")

    # 1. Check if already mapped (ProductMapping) → conflict
    mapping_exists = db.query(ProductMapping).filter(ProductMapping.aliexpress_id == aliexpress_id).first()
    if mapping_exists:
        raise HTTPException(
            409,
            detail=f"AliExpress ID {aliexpress_id} is already mapped to Shopify product {mapping_exists.shopify_product_id}. "
                   f"Use the 'Sync Mappings' page to update its price – do not import again."
        )

    # 2. Check local imported product
    existing = db.query(ImportedProduct).filter(ImportedProduct.aliexpress_id == aliexpress_id).first()
    if existing and existing.shopify_product_id:
        raise HTTPException(409, "Product already imported to Shopify")

    # 3. Duplicate check by title in Shopify (if product already exists)
    title = raw_product.get("title")
    created = False
    shopify_id = None
    shopify_status = "draft"

    if title and check_product_exists_in_shopify(title):
        # Find existing Shopify product by title and link it
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
                # Update existing local record if any
                if existing:
                    existing.shopify_product_id = shopify_id
                    existing.shopify_status = shopify_status
                    # Update other fields
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
                    return existing, False
                else:
                    # Create local record only (no new Shopify product)
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
                    return new_product, False
        except Exception as e:
            print(f"Error checking Shopify product: {e}")
            raise HTTPException(409, f"Product '{title}' already exists in Shopify (title match), but could not retrieve its ID. Please contact support.")

    # 4. Product not found in Shopify → create new
    shopify_resp = create_shopify_product(raw_product)
    shopify_product_new = shopify_resp.get("product", {})
    shopify_id = str(shopify_product_new.get("id"))
    shopify_status = shopify_product_new.get("status", "draft")
    created = True

    if existing:
        existing.shopify_product_id = shopify_id
        existing.shopify_status = shopify_status
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
        return existing, created
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
        return new_product, created

# main.py

# def process_pending_imports():
#     """Check each pending product for stock. If in stock, import to Shopify."""
#     db = SessionLocal()
#     try:
#         pendings = db.query(PendingImport).filter(PendingImport.status == "pending").all()
#         if not pendings:
#             return

#         for pending in pendings:
#             aliexpress_id = pending.aliexpress_id
#             try:
#                 # Fetch latest data
#                 raw_product = get_product(aliexpress_id, db)
#                 pending.last_checked = func.now()

#                 if is_product_in_stock(raw_product):
#                     # Now in stock → import
#                     product, created = import_aliexpress_product_to_shopify(raw_product, db)
#                     # Mark pending as processed
#                     pending.status = "imported"
#                     db.commit()
#                     print(f"[Pending] Imported {aliexpress_id} (Shopify ID {product.shopify_product_id})")
#                 else:
#                     # Still out of stock – update last_checked and retry count
#                     pending.retry_count += 1
#                     db.commit()
#                     print(f"[Pending] {aliexpress_id} still out of stock (retry {pending.retry_count})")
#             except Exception as e:
#                 print(f"[Pending] Error processing {aliexpress_id}: {e}")
#                 # Optionally mark as failed after too many attempts
#                 # pending.status = "failed"
#                 # db.commit()
#     finally:
#         db.close()

def process_pending_imports():
    """
    Background job: for products already in Shopify but with OOS variants,
    check if stock has returned and push inventory update.
    """
    from sqlalchemy.sql import func as sqlfunc
    db = SessionLocal()
    try:
        pendings = db.query(PendingImport).filter(
            PendingImport.status == "pending"
        ).all()

        if not pendings:
            print("[Pending] No pending records to process")
            return

        print(f"[Pending] Processing {len(pendings)} pending record(s)")

        for pending in pendings:
            aliexpress_id = pending.aliexpress_id
            try:
                raw_product = get_product(aliexpress_id, db)
                stock_info = classify_skus(raw_product)

                pending.out_of_stock_skus = stock_info["out_of_stock"]
                pending.in_stock_skus = stock_info["in_stock"]
                pending.last_checked = sqlfunc.now()
                pending.retry_count = (pending.retry_count or 0) + 1

                print(f"[Pending] {aliexpress_id}: "
                      f"in_stock={len(stock_info['in_stock'])} "
                      f"out_of_stock={len(stock_info['out_of_stock'])}")

                # Find the linked ImportedProduct to get shopify_product_id
                imported = db.query(ImportedProduct).filter(
                    ImportedProduct.aliexpress_id == aliexpress_id
                ).first()

                if not imported or not imported.shopify_product_id:
                    # Product not imported yet (legacy behavior) — try to import
                    try:
                        product, created = import_aliexpress_product_to_shopify(raw_product, db)
                        if not stock_info["out_of_stock"]:
                            pending.status = "imported"
                        db.commit()
                        print(f"[Pending] ✓ Imported {aliexpress_id} → Shopify {product.shopify_product_id}")
                    except HTTPException as e:
                        if e.status_code == 409:
                            pending.status = "imported"
                            db.commit()
                        else:
                            db.commit()
                            print(f"[Pending] Import failed for {aliexpress_id}: {e.detail}")
                    continue

                # Product already in Shopify — push inventory update
                skus = raw_product.get("skus", [])
                if skus:
                    from .shopify import update_shopify_product_inventory_with_skus
                    inv_updated = update_shopify_product_inventory_with_skus(
                        imported.shopify_product_id, skus
                    )
                    print(f"[Pending] Inventory sync for {aliexpress_id}: updated={inv_updated}")

                # If all variants now have stock → mark done
                if not stock_info["out_of_stock"] and not stock_info["unknown"]:
                    pending.status = "imported"
                    print(f"[Pending] ✓ All variants in stock — marking {aliexpress_id} as imported")
                elif stock_info["in_stock"] and stock_info["out_of_stock"]:
                    pending.status = "partial"
                    print(f"[Pending] {aliexpress_id} partially restocked — {len(stock_info['out_of_stock'])} still OOS")
                else:
                    print(f"[Pending] {aliexpress_id} still fully OOS ({len(stock_info['out_of_stock'])} variants)")

                db.commit()

            except Exception as e:
                print(f"[Pending] Error processing {aliexpress_id}: {e}")
                db.commit()
    finally:
        db.close()

# main.py

@app.post("/admin/process-pending")
def process_pending_now():
    """Manually trigger pending import processing."""
    process_pending_imports()
    return {"message": "Pending imports processed"}

# main.py

# ═══════════════════════════════════════════════════════════════════════
# FIXED /pending/list endpoint for main.py
#
# The frontend expects these fields on each pending record:
#   id, aliexpress_id, title, main_image, total_skus,
#   out_of_stock_skus, in_stock_skus, status,
#   created_at, last_checked, retry_count
#
# Replace your existing @app.get("/pending/list") with this.
# ═══════════════════════════════════════════════════════════════════════

@app.get("/pending/list")
def list_pending_imports(db: Session = Depends(get_db)):
    pendings = db.query(PendingImport).order_by(PendingImport.created_at.desc()).all()
    result = []
    for p in pendings:
        # product_data may be None if something went wrong during save
        product_data = p.product_data or {}

        # Safely extract title and image from the nested product_data JSON
        title      = product_data.get("title") or "—"
        main_image = product_data.get("main_image")

        # out_of_stock_skus / in_stock_skus may not exist on old rows
        # (before the column was added) — fall back to empty list safely
        oos_skus = p.out_of_stock_skus or []
        ins_skus = p.in_stock_skus     or []

        # total_skus: prefer explicit count, fall back to summing both lists
        total_skus = len(product_data.get("skus") or []) or (len(oos_skus) + len(ins_skus))

        result.append({
            "id":                p.id,
            "aliexpress_id":     p.aliexpress_id,
            "title":             title,
            "main_image":        main_image,
            "total_skus":        total_skus,
            "out_of_stock_skus": oos_skus,
            "in_stock_skus":     ins_skus,
            "status":            p.status,
            "created_at":        p.created_at.isoformat() if p.created_at else None,
            "last_checked":      p.last_checked.isoformat() if p.last_checked else None,
            "retry_count":       p.retry_count or 0,
        })

    return {"pendings": result}




@app.post("/pending/retry")
def retry_pending_import(aliexpress_id: str, db: Session = Depends(get_db)):
    pending = db.query(PendingImport).filter(
        PendingImport.aliexpress_id == aliexpress_id,
        PendingImport.status.in_(["pending", "partial", "failed"]),
    ).first()
    if not pending:
        raise HTTPException(404, "No active pending record found for this ID")

    raw = get_product(aliexpress_id, db)
    stock_info = classify_skus(raw)

    pending.out_of_stock_skus = stock_info["out_of_stock"]
    pending.in_stock_skus = stock_info["in_stock"]
    pending.last_checked = sqlfunc.now()
    pending.retry_count = (pending.retry_count or 0) + 1
    db.commit()

    # Find linked imported product
    imported = db.query(ImportedProduct).filter(
        ImportedProduct.aliexpress_id == aliexpress_id
    ).first()

    if not imported or not imported.shopify_product_id:
        # Fallback: product not yet in Shopify, try importing now
        try:
            product, created = import_aliexpress_product_to_shopify(raw, db)
            if not stock_info["out_of_stock"]:
                pending.status = "imported"
            db.commit()
            return {
                "status": "imported",
                "message": f"Product imported to Shopify (ID: {product.shopify_product_id})",
                "shopify_product_id": product.shopify_product_id,
                "out_of_stock_skus": stock_info["out_of_stock"],
                "can_retry": bool(stock_info["out_of_stock"]),
            }
        except HTTPException as e:
            if e.status_code == 409:
                pending.status = "imported"
                db.commit()
                return {"status": "imported", "message": "Already in Shopify.", "out_of_stock_skus": [], "can_retry": False}
            raise e

    # Product is in Shopify — push inventory update
    skus = raw.get("skus", [])
    inv_updated = False
    if skus:
        from .shopify import update_shopify_product_inventory_with_skus
        inv_updated = update_shopify_product_inventory_with_skus(imported.shopify_product_id, skus)

    oos = stock_info["out_of_stock"]
    ins = stock_info["in_stock"]

    if not oos and not stock_info["unknown"]:
        pending.status = "imported"
        db.commit()
        return {
            "status": "imported",
            "message": "All variants are now in stock — inventory updated in Shopify!",
            "out_of_stock_skus": [],
            "inventory_updated": inv_updated,
            "can_retry": False,
        }
    elif ins and oos:
        pending.status = "partial"
        db.commit()
    else:
        db.commit()

    oos_labels = [s.get("label") or s.get("sku_id") or "—" for s in oos]
    return {
        "status": "oos",
        "message": (
            f"{len(oos)} variant(s) still out of stock. "
            f"Inventory synced where stock is available. "
            f"Will auto-update when remaining variants restock."
        ),
        "out_of_stock_skus": oos,
        "out_of_stock_labels": oos_labels,
        "in_stock_count": len(ins),
        "oos_count": len(oos),
        "inventory_updated": inv_updated,
        "can_retry": True,
    }





@app.delete("/pending/{pending_id}")
def delete_pending_import(pending_id: int, db: Session = Depends(get_db)):
    pending = db.query(PendingImport).filter(PendingImport.id == pending_id).first()
    if not pending:
        raise HTTPException(404, "Pending record not found")
    db.delete(pending)
    db.commit()
    return {"message": "Pending record deleted"}


def classify_skus(product_data: dict) -> dict:
    skus = product_data.get("skus") or []

    if not skus:
        return {
            "in_stock": [], "out_of_stock": [], "unknown": [],
            "any_in_stock": False,   # no known stock → not in stock
            "all_in_stock": False,
            "any_oos": False,
            "no_info": True,
        }

    in_stock, out_of_stock, unknown = [], [], []

    for sku in skus:
        stock_val = sku.get("stock")
        try:
            stock = int(stock_val) if stock_val is not None else None
        except (ValueError, TypeError):
            stock = None

        entry = {
            "sku_id":     sku.get("sku_id"),
            "label":      sku.get("label") or sku.get("sku_attr") or "—",
            "stock":      stock,
            "sale_price": sku.get("sale_price"),
            "price":      sku.get("price"),
        }

        if stock is None:
            unknown.append(entry)
        elif stock > 0:
            in_stock.append(entry)
        else:
            out_of_stock.append(entry)

    no_info = (len(unknown) == len(skus) and len(out_of_stock) == 0)

    return {
        "in_stock": in_stock,
        "out_of_stock": out_of_stock,
        "unknown": unknown,
        "any_in_stock": len(in_stock) > 0,   # no longer uses no_info
        "all_in_stock": len(out_of_stock) == 0 and len(unknown) == 0,
        "any_oos": len(out_of_stock) > 0,
        "no_info": no_info,
    }





def upsert_pending(aliexpress_id: str, raw_product: dict,
                   stock_info: dict, status: str, db) -> PendingImport:
    """Create or update a PendingImport row."""
    record = db.query(PendingImport).filter(
        PendingImport.aliexpress_id == aliexpress_id
    ).first()
 
    if record:
        record.product_data      = raw_product
        record.out_of_stock_skus = stock_info["out_of_stock"]
        record.in_stock_skus     = stock_info["in_stock"]
        record.status            = status
        record.retry_count       = 0
    else:
        record = PendingImport(
            aliexpress_id     = aliexpress_id,
            product_data      = raw_product,
            out_of_stock_skus = stock_info["out_of_stock"],
            in_stock_skus     = stock_info["in_stock"],
            status            = status,
        )
        db.add(record)
 
    db.commit()
    db.refresh(record)
    return record
 

@app.post("/admin/requeue-partial-imports")
def requeue_partial_imports(db: Session = Depends(get_db)):
    """Re-queue imported products that still have OOS variants so they get re-checked."""
    pendings = db.query(PendingImport).filter(PendingImport.status == "imported").all()
    requeued = 0
    for p in pendings:
        oos = p.out_of_stock_skus or []
        if len(oos) > 0:
            p.status = "partial"
            requeued += 1
    db.commit()
    return {"message": f"Re-queued {requeued} partially-imported products", "requeued": requeued}



@app.get("/debug/scheduler-status")
def scheduler_status():
    if scheduler is None:
        return {"running": False, "message": "Scheduler not started"}
    jobs = scheduler.get_jobs()
    return {
        "running": True,
        "jobs": [
            {
                "id": j.id,
                "next_run_time": str(j.next_run_time),
                "trigger": str(j.trigger),
            }
            for j in jobs
        ]
    }

@app.post("/admin/backfill-oos-to-pending")
def backfill_oos_to_pending(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Scan all imported products, find those with OOS variants,
    and add them to the pending queue for inventory tracking.
    """
    def run():
        inner_db = SessionLocal()
        try:
            products = inner_db.query(ImportedProduct).filter(
                ImportedProduct.shopify_product_id.isnot(None)
            ).all()

            print(f"[Backfill] Scanning {len(products)} imported products for OOS variants...")
            queued = 0
            skipped = 0
            errors = 0

            for prod in products:
                try:
                    # Skip if already in pending queue
                    existing = inner_db.query(PendingImport).filter(
                        PendingImport.aliexpress_id == prod.aliexpress_id
                    ).first()
                    if existing:
                        skipped += 1
                        continue

                    # Fetch fresh AliExpress data
                    raw = get_product(prod.aliexpress_id, inner_db)
                    stock_info = classify_skus(raw)

                    # Only queue if there are actual OOS variants (not just unknown)
                    if stock_info["out_of_stock"]:
                        upsert_pending(
                            prod.aliexpress_id, raw, stock_info, "pending", inner_db
                        )
                        queued += 1
                        print(f"[Backfill] Queued {prod.aliexpress_id} — "
                              f"{len(stock_info['out_of_stock'])} OOS variant(s)")
                    else:
                        skipped += 1

                except Exception as e:
                    errors += 1
                    print(f"[Backfill] Error for {prod.aliexpress_id}: {e}")

            print(f"[Backfill] Done — queued={queued}, skipped={skipped}, errors={errors}")
        finally:
            inner_db.close()

    background_tasks.add_task(run)
    return {"message": "Backfill started in background — check terminal for progress"}


# ══════════════════════════════════════════════════════════
# ADD THIS ENDPOINT to main.py  (after /admin/backfill-sku-metafields)
# ══════════════════════════════════════════════════════════

@app.post("/admin/backfill-sku-images")
def backfill_sku_images_endpoint(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Scan every imported product that has a Shopify ID.
    For each one, fetch fresh AliExpress data (which now includes per-SKU images),
    then call backfill_sku_images() to upload missing variant images to Shopify.
    Runs in the background — check terminal logs for progress.
    """
    def run():
        from .database import SessionLocal
        from .shopify import backfill_sku_images
        from .aliexpress import get_product as ali_get_product

        inner_db = SessionLocal()
        try:
            products = inner_db.query(ImportedProduct).filter(
                ImportedProduct.shopify_product_id.isnot(None)
            ).all()

            print(f"[BackfillImages] Scanning {len(products)} product(s)…")
            total_attached = 0
            total_skipped  = 0
            errors = 0

            for prod in products:
                try:
                    # Fetch fresh data so we get the latest sku[].image URLs
                    raw = ali_get_product(prod.aliexpress_id, inner_db)
                    skus = raw.get("skus", [])

                    # Quick check: does this product have any sku images at all?
                    has_images = any(s.get("image") for s in skus)
                    if not has_images:
                        print(f"[BackfillImages] {prod.aliexpress_id} — no SKU images in AliExpress data, skipping")
                        total_skipped += 1
                        continue

                    result = backfill_sku_images(prod.shopify_product_id, skus)
                    total_attached += result["attached"]
                    total_skipped  += result["skipped"]
                    print(
                        f"[BackfillImages] {prod.aliexpress_id} → "
                        f"attached={result['attached']} skipped={result['skipped']} "
                        f"total_variants={result['total_variants']}"
                    )
                except Exception as e:
                    errors += 1
                    print(f"[BackfillImages] Error for {prod.aliexpress_id}: {e}")

            print(
                f"[BackfillImages] Done — "
                f"total_attached={total_attached} total_skipped={total_skipped} errors={errors}"
            )
        finally:
            inner_db.close()

    background_tasks.add_task(run)
    return {"message": "SKU image backfill started in background — check terminal for progress"}


# ══════════════════════════════════════════════════════════
# ADD THIS ENDPOINT to main.py  (single-product image sync)
# Lets the frontend trigger a per-product image sync from the table
# ══════════════════════════════════════════════════════════

@app.post("/dashboard/products/{product_id}/sync-images")
def sync_product_images(product_id: int, db: Session = Depends(get_db)):
    """
    Fetch fresh AliExpress SKU data for this product and attach any
    missing variant images to the linked Shopify product.
    """
    from .shopify import backfill_sku_images
    from .aliexpress import get_product as ali_get_product

    product = db.query(ImportedProduct).filter(ImportedProduct.id == product_id).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if not product.shopify_product_id:
        raise HTTPException(400, "Product has no Shopify ID linked")

    try:
        raw  = ali_get_product(product.aliexpress_id, db)
        skus = raw.get("skus", [])
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch AliExpress data: {e}")

    result = backfill_sku_images(product.shopify_product_id, skus)
    return {
        "message": (
            f"Attached {result['attached']} image(s). "
            f"{result['skipped']} variant(s) already had images."
        ),
        **result,
    }