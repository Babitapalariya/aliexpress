import requests
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from .aliexpress import get_product, search_products
from .auth import router as auth_router
from .config import get_settings
from .database import Base, engine, get_db
from . import models
from .models import ImportedProduct
from .shopify import create_shopify_product, check_product_exists_in_shopify

settings = get_settings()

app = FastAPI(
    title="AliExpress Shopify Middleware",
    description="Middleware API for importing AliExpress products into Shopify.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:3001", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


# ─── General ─────────────────────────────────────────────────

@app.get("/")
def home():
    return {"status": "running"}


@app.get("/api/docs", include_in_schema=False)
def api_docs():
    return RedirectResponse("/docs")


# ─── AliExpress ───────────────────────────────────────────────

@app.get("/product/{product_id}")
def product(product_id: str, db: Session = Depends(get_db)):
    """Fetch a single AliExpress product by ID."""
    return get_product(product_id, db)


@app.get("/search")
def search(keyword: str, page: int = 1, page_size: int = 20, db: Session = Depends(get_db)):
    """Search AliExpress products by keyword."""
    return search_products(keyword, page, page_size, db)


# ─── Import ───────────────────────────────────────────────────

@app.post("/import/{product_id}")
def import_product(product_id: str, db: Session = Depends(get_db)):
    """
    1. Fetch product from AliExpress.
    2. Check Shopify for duplicate by title → 409 if exists.
    3. Create product in Shopify as draft.
    4. Save record to imported_products table.
    """
    product_data = get_product(product_id, db)
    result       = create_shopify_product(product_data)   # raises 409 if duplicate

    shopify_id = str(result.get("product", {}).get("id", ""))

    # Save to DB
    record = ImportedProduct(
        aliexpress_id      = str(product_data.get("product_id") or product_id),
        original_title     = product_data.get("title"),
        original_price     = str(product_data.get("original_price") or ""),
        sale_price         = str(product_data.get("sale_price") or ""),
        currency           = product_data.get("currency") or "USD",
        main_image         = product_data.get("main_image"),
        product_url        = product_data.get("product_url"),
        store_name         = product_data.get("store_name"),
        avg_rating         = str(product_data.get("avg_rating") or ""),
        review_count       = str(product_data.get("review_count") or ""),
        orders             = str(product_data.get("orders") or ""),
        sku_count          = product_data.get("sku_count") or 0,
        shopify_product_id = shopify_id,
        shopify_status     = "draft",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return {**result, "db_id": record.id}


# ─── Dashboard — imported products ───────────────────────────

@app.get("/dashboard/products")
def dashboard_products(
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    """Return all imported products with pagination."""
    total   = db.query(ImportedProduct).count()
    records = (
        db.query(ImportedProduct)
        .order_by(ImportedProduct.imported_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    def serialize(r: ImportedProduct):
        return {
            "id":                  r.id,
            "aliexpress_id":       r.aliexpress_id,
            "title":               r.custom_title or r.original_title,
            "original_title":      r.original_title,
            "custom_title":        r.custom_title,
            "price":               r.custom_price or r.sale_price,
            "original_price":      r.original_price,
            "sale_price":          r.sale_price,
            "custom_price":        r.custom_price,
            "currency":            r.currency,
            "main_image":          r.main_image,
            "product_url":         r.product_url,
            "store_name":          r.store_name,
            "rating":              r.custom_rating or r.avg_rating,
            "avg_rating":          r.avg_rating,
            "custom_rating":       r.custom_rating,
            "review_count":        r.review_count,
            "orders":              r.orders,
            "sku_count":           r.sku_count,
            "shopify_product_id":  r.shopify_product_id,
            "shopify_status":      r.shopify_status,
            "custom_description":  r.custom_description,
            "imported_at":         r.imported_at.isoformat() if r.imported_at else None,
            "updated_at":          r.updated_at.isoformat()  if r.updated_at  else None,
        }

    return {
        "total":    total,
        "page":     page,
        "pages":    -(-total // page_size),  # ceiling division
        "products": [serialize(r) for r in records],
    }


@app.get("/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db)):
    """Return summary stats for the dashboard header cards."""
    total    = db.query(ImportedProduct).count()
    draft    = db.query(ImportedProduct).filter(ImportedProduct.shopify_status == "draft").count()
    active   = db.query(ImportedProduct).filter(ImportedProduct.shopify_status == "active").count()
    modified = db.query(ImportedProduct).filter(
        (ImportedProduct.custom_title    != None) |  # noqa: E711
        (ImportedProduct.custom_price    != None) |
        (ImportedProduct.custom_rating   != None) |
        (ImportedProduct.custom_description != None)
    ).count()
    return {
        "total_imported": total,
        "draft":          draft,
        "active":         active,
        "modified":       modified,
    }


# ─── Modify product ───────────────────────────────────────────

class ProductUpdateRequest(BaseModel):
    custom_title:       Optional[str] = None
    custom_price:       Optional[str] = None
    custom_description: Optional[str] = None
    custom_rating:      Optional[str] = None


@app.put("/dashboard/products/{db_id}")
def update_product(
    db_id: int,
    body: ProductUpdateRequest,
    db: Session = Depends(get_db),
):
    """
    Update user-editable fields on an imported product.
    Also pushes the updated title + price to Shopify.
    """
    record = db.query(ImportedProduct).filter(ImportedProduct.id == db_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Product not found in database.")

    # Update DB fields
    if body.custom_title       is not None: record.custom_title       = body.custom_title
    if body.custom_price       is not None: record.custom_price       = body.custom_price
    if body.custom_description is not None: record.custom_description = body.custom_description
    if body.custom_rating      is not None: record.custom_rating      = body.custom_rating

    db.commit()
    db.refresh(record)

    # Push changes to Shopify if product was imported there
    shopify_result = None
    if record.shopify_product_id:
        try:
            from .shopify import _get_shopify_token
            token = _get_shopify_token()
            shop  = settings.SHOPIFY_STORE.replace(".myshopify.com", "").strip()
            url   = (
                f"https://{shop}.myshopify.com/admin/api/"
                f"{settings.SHOPIFY_API_VERSION}/products/{record.shopify_product_id}.json"
            )
            payload: dict = {}
            if body.custom_title:       payload["title"]     = body.custom_title
            if body.custom_description: payload["body_html"] = body.custom_description
            if body.custom_price:
                payload["variants"] = [{"price": body.custom_price}]

            if payload:
                res = requests.put(
                    url,
                    json={"product": payload},
                    headers={
                        "X-Shopify-Access-Token": token,
                        "Content-Type":           "application/json",
                    },
                    timeout=20,
                )
                res.raise_for_status()
                shopify_result = res.json()
        except Exception as e:
            shopify_result = {"error": str(e)}

    return {
        "message":        "Product updated successfully",
        "db_id":          record.id,
        "shopify_synced": shopify_result is not None and "error" not in (shopify_result or {}),
        "shopify_result": shopify_result,
        "product": {
            "id":                 record.id,
            "title":              record.custom_title or record.original_title,
            "price":              record.custom_price or record.sale_price,
            "rating":             record.custom_rating or record.avg_rating,
            "custom_description": record.custom_description,
        },
    }


@app.delete("/dashboard/products/{db_id}")
def delete_product(db_id: int, db: Session = Depends(get_db)):
    """Remove an imported product record from the database."""
    record = db.query(ImportedProduct).filter(ImportedProduct.id == db_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Product not found.")
    db.delete(record)
    db.commit()
    return {"message": "Deleted", "db_id": db_id}
