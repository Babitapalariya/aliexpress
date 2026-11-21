"""Exercise Shopify scans against an isolated database, without live services."""
import unittest
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, ImportedProduct, ProductMapping
from test_variant_price_sync import load_main_functions


class ShopifyScanDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        self.fetch = Mock(return_value={"title": "Helmet", "sale_price": "46.39"})
        self.ns = dict(
            SessionLocal=self.sessions, ImportedProduct=ImportedProduct,
            ProductMapping=ProductMapping, get_product=self.fetch,
            get_all_shopify_imported_products=lambda: [dict(
                aliexpress_id="3256809143926917", shopify_id="9006864498825",
                title="Helmet", status="draft")],
            _safe_int=lambda value: int(value or 0),
        )
        load_main_functions(self.ns, "sync_existing_shopify_products_to_db")

    def scan(self):
        self.ns["sync_existing_shopify_products_to_db"]()
        with self.sessions() as db:
            return db.query(ImportedProduct).count()

    def add_mapping(self, supplier="3256809143926917", shopify="9006864498825"):
        with self.sessions() as db:
            db.add(ProductMapping(aliexpress_id=supplier, shopify_product_id=shopify))
            db.commit()

    def test_mapping_supplier_id_blocks_scan(self):
        self.add_mapping(shopify="other")
        self.assertEqual(self.scan(), 0)
        self.fetch.assert_not_called()

    def test_mapping_shopify_id_blocks_scan_after_remap(self):
        self.add_mapping(supplier="new-supplier")
        self.assertEqual(self.scan(), 0)
        self.fetch.assert_not_called()

    def test_mapping_added_during_supplier_fetch_blocks_insert(self):
        def fetch(*args):
            self.add_mapping()
            return {"title": "Helmet"}
        self.fetch.side_effect = fetch
        self.assertEqual(self.scan(), 0)

    def test_unmapped_product_imports_once(self):
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.scan(), 1)
        self.fetch.assert_called_once()

    def test_existing_import_with_changed_supplier_is_not_recreated(self):
        with self.sessions() as db:
            db.add(ImportedProduct(aliexpress_id="new-supplier", original_title="Helmet",
                                   shopify_product_id="9006864498825"))
            db.commit()
        self.assertEqual(self.scan(), 1)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
