from sqlalchemy import Column, DateTime, Float, Integer, String, Text, func
from .database import Base


class AliExpressToken(Base):
    __tablename__ = "aliexpress_tokens"

    id            = Column(Integer, primary_key=True, index=True)
    access_token  = Column(String(500), nullable=False)
    refresh_token = Column(String(500), nullable=True)
    expires_in    = Column(Integer, nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ImportedProduct(Base):
    """
    Stores every product that has been successfully imported into Shopify.
    Users can modify title, price, description and rating from the dashboard.
    """
    __tablename__ = "imported_products"

    id                 = Column(Integer, primary_key=True, index=True)

    # ── AliExpress data ──────────────────────────
    aliexpress_id      = Column(String(100), nullable=False, index=True)
    original_title     = Column(String(500), nullable=True)
    original_price     = Column(String(50),  nullable=True)
    sale_price         = Column(String(50),  nullable=True)
    currency           = Column(String(10),  nullable=True, default="USD")
    main_image         = Column(Text,        nullable=True)
    product_url        = Column(Text,        nullable=True)
    store_name         = Column(String(200), nullable=True)
    avg_rating         = Column(String(20),  nullable=True)
    review_count       = Column(String(50),  nullable=True)
    orders             = Column(String(50),  nullable=True)
    sku_count          = Column(Integer,     nullable=True)

    # ── Shopify data ──────────────────────────────
    shopify_product_id = Column(String(100), nullable=True, index=True)
    shopify_status     = Column(String(20),  nullable=True, default="draft")  # draft / active

    # ── User-editable fields ──────────────────────
    custom_title       = Column(String(500), nullable=True)   # overrides original_title
    custom_price       = Column(String(50),  nullable=True)   # overrides sale_price
    custom_description = Column(Text,        nullable=True)
    custom_rating      = Column(String(20),  nullable=True)   # overrides avg_rating

    # ── Timestamps ───────────────────────────────
    imported_at        = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at         = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
