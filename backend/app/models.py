from sqlalchemy import Column, DateTime, Integer, String, Float, Boolean, Text, JSON, func
from .database import Base


class AliExpressToken(Base):
    __tablename__ = "aliexpress_tokens"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(String(500), nullable=False)
    refresh_token = Column(String(500), nullable=True)
    expires_in = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

class ImportedProduct(Base):
    __tablename__ = "imported_products"

    id = Column(Integer, primary_key=True, index=True)
    aliexpress_id = Column(String(64), unique=True, nullable=False, index=True)
    original_title = Column(String(500), nullable=False)
    custom_title = Column(String(500), nullable=True)
    original_price = Column(String(32), nullable=True)
    custom_price = Column(String(32), nullable=True)
    currency = Column(String(8), nullable=True)
    original_description = Column(Text, nullable=True)
    custom_description = Column(Text, nullable=True)
    main_image = Column(String(500), nullable=True)
    all_images = Column(JSON, nullable=True)          # list of URLs
    store_name = Column(String(200), nullable=True)
    avg_rating = Column(Float, nullable=True)
    custom_rating = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    orders = Column(Integer, nullable=True)
    sku_count = Column(Integer, nullable=True)
    skus = Column(JSON, nullable=True)                # store full SKU array
    shopify_product_id = Column(String(64), nullable=True)
    shopify_status = Column(String(20), default="draft")   # draft / active
    imported_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    track_price = Column(Boolean, default=True, nullable=False)
    price_increase = Column(Float, default=0.0, nullable=False)  # amount in USD
    shipping_cost = Column(String(32), nullable=True)
    shipping_method = Column(String(100), nullable=True)
    total_stock = Column(Integer, nullable=True)
    last_shipment_fetch = Column(DateTime(timezone=True), nullable=True)
    price_mode = Column(String(20), default='auto', nullable=False)

 
class ProductMapping(Base):
    __tablename__ = "product_mappings"

    id = Column(Integer, primary_key=True, index=True)
    aliexpress_id = Column(String(64), unique=True, nullable=False, index=True)
    shopify_product_id = Column(String(64), nullable=False, index=True)
    shopify_product_title = Column(String(500), nullable=True)  # optional, for display
    track_price = Column(Boolean, default=True, nullable=False)
    price_mode = Column(String(20), default='auto', nullable=False)
    price_increase = Column(Float, default=0.0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# models.py (add after ImportedProduct)

 
class PendingImport(Base):
    __tablename__ = "pending_imports"

    id            = Column(Integer, primary_key=True, index=True)
    aliexpress_id = Column(String(64), unique=True, nullable=False, index=True)
    product_data  = Column(JSON, nullable=False)   # full product dict from get_product()
    out_of_stock_skus = Column(JSON, nullable=True)  # NEW: list of {sku_id, label, stock}
    in_stock_skus     = Column(JSON, nullable=True)  # NEW: skus that ARE in stock
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    last_checked  = Column(DateTime(timezone=True), nullable=True)
    retry_count   = Column(Integer, default=0)
    status        = Column(String(20), default='pending')  # pending / imported / failed
    