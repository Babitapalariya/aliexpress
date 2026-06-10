from app.database import SessionLocal
from app.models import ProductMapping
from app.aliexpress import get_product
from app.shopify import store_aliexpress_sku_ids

db = SessionLocal()
mappings = db.query(ProductMapping).all()
for m in mappings:
    print(f"Processing {m.aliexpress_id}")
    raw = get_product(m.aliexpress_id, db)
    skus = raw.get("skus", [])
    if skus:
        store_aliexpress_sku_ids(m.shopify_product_id, skus)
db.close()