import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import shopify_links as links


class ShopifyLinkTests(unittest.TestCase):
    def setUp(self):
        links._cache.clear()
        self.settings = patch.object(links, "get_settings", return_value=SimpleNamespace(SHOPIFY_STORE="test-store.myshopify.com"))
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def test_store_scoped_numeric_and_gid_urls(self):
        expected = "https://test-store.myshopify.com/admin/products/123"
        for store in ("test-store", "test-store.myshopify.com", "https://test-store.myshopify.com/"):
            self.assertEqual(links.shopify_product_url("123", store), expected)
            self.assertEqual(links.shopify_product_url("gid://shopify/Product/123", store), expected)
        self.assertEqual(links.open_shopify_product("123").headers["location"], expected)

    def test_invalid_ids_and_stores_cannot_create_links(self):
        for ident in (None, "", "0", "123.0", "gid://shopify/ProductVariant/123", "123/../../orders"):
            self.assertIsNone(links.shopify_product_url(ident))
        for store in ("", "example.org", "x.myshopify.com.evil.com", "x/admin"):
            self.assertIsNone(links.shopify_product_url("123", store))

    def test_missing_product_hides_link_without_rewriting_ids(self):
        rows = [dict(id=7, shopify_product_id="123", aliexpress_id="456")]
        with patch("app.shopify._graphql", return_value={"nodes": [None]}):
            links.verify_flagged_shopify_links(rows)
        self.assertEqual(rows[0]["shopify_link_status"], "missing")
        self.assertIsNone(rows[0]["shopify_url"])
        self.assertEqual((rows[0]["id"], rows[0]["shopify_product_id"], rows[0]["aliexpress_id"]), (7, "123", "456"))

    def test_valid_mapping_gets_link_and_missing_title(self):
        rows = [dict(shopify_product_id="123", aliexpress_id="456", title="")]
        with patch("app.shopify._graphql", return_value={"nodes": [dict(id="gid://shopify/Product/123", title="Tire", supplier={"value": "456"})]}) as query:
            links.verify_flagged_shopify_links(rows)
            links.verify_flagged_shopify_links(rows)
        query.assert_called_once()
        self.assertEqual(rows[0]["shopify_link_status"], "verified")
        self.assertEqual(rows[0]["title"], "Tire")
        self.assertEqual(rows[0]["shopify_url"], "https://test-store.myshopify.com/admin/products/123")

    def test_api_failure_is_not_reported_as_deleted(self):
        for result in ({}, {"nodes": []}, {"nodes": [{"id": "gid://shopify/Product/999"}]}):
            rows = [dict(shopify_product_id="123", aliexpress_id="456")]
            with patch("app.shopify._graphql", return_value=result):
                links.verify_flagged_shopify_links(rows)
            self.assertEqual(rows[0]["shopify_link_status"], "unverified")
            self.assertIsNotNone(rows[0]["shopify_url"])

    def test_supplier_mismatch_is_reported_without_remapping(self):
        rows = [dict(shopify_product_id="123", aliexpress_id="456")]
        with patch("app.shopify._graphql", return_value={"nodes": [dict(id="gid://shopify/Product/123", supplier={"value": "789"})]}):
            links.verify_flagged_shopify_links(rows)
        self.assertEqual(rows[0]["shopify_link_status"], "supplier_mismatch")
        self.assertEqual(rows[0]["aliexpress_id"], "456")

    def test_flagged_endpoints_preserve_database_ownership(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.models import Base, ImportedProduct, ProductMapping
        from test_variant_price_sync import load_main_functions
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        ns = dict(ImportedProduct=ImportedProduct, ProductMapping=ProductMapping)
        load_main_functions(ns, "get_dead_listings", "get_dead_mappings")
        with Session(engine) as db:
            db.add(ImportedProduct(aliexpress_id="456", shopify_product_id="123", original_title="Tire", is_dead_listing=True))
            db.add(ProductMapping(aliexpress_id="789", shopify_product_id="124", is_dead_listing=True))
            db.commit()
            with patch("app.shopify._graphql", return_value={"nodes": [None]}):
                first = ns["get_dead_listings"](db)
                second = ns["get_dead_mappings"](db)
            self.assertEqual(first["products"][0]["shopify_product_id"], "123")
            self.assertEqual(second["mappings"][0]["shopify_product_id"], "124")
            self.assertFalse(db.dirty)
            self.assertEqual(first["products"][0]["shopify_link_status"], "missing")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
