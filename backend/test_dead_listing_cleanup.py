import unittest
from unittest.mock import Mock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.models import Base, ImportedProduct, ProductMapping, PendingImport, AliExpressIdHistory
from app.dead_listing_cleanup import delete_dead_listings


class DeadCleanupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.db.add_all([
            ImportedProduct(id=1, aliexpress_id="111", shopify_product_id="501", original_title="Dead", is_dead_listing=True),
            ImportedProduct(id=2, aliexpress_id="222", shopify_product_id="502", original_title="Alive", is_dead_listing=False),
            ProductMapping(id=1, aliexpress_id="333", shopify_product_id="503", is_dead_listing=True),
            PendingImport(aliexpress_id="111", product_data={}),
            AliExpressIdHistory(source="imported", record_id=1, aliexpress_id="old"),
            AliExpressIdHistory(source="mapping", record_id=1, aliexpress_id="other-old"),
        ])
        self.db.commit()
        self.fetch = Mock(side_effect=lambda aid, db: {"product_id": aid, "skus": [], "sale_price": None})
        self.is_dead = lambda raw: not raw.get("sale_price")
        self.query = patch("app.dead_listing_cleanup.shopify._graphql", return_value={"product": None})
        self.graphql = self.query.start()
        self.addCleanup(self.query.stop)

    def run_delete(self, items=None):
        return delete_dead_listings(self.db, items or [{"source": "imported", "id": 1}], self.fetch, self.is_dead)

    def test_delete_is_local_and_scoped_with_queue_cleanup(self):
        result = self.run_delete()
        self.assertEqual(len(result["deleted"]), 1)
        self.assertIsNone(self.db.get(ImportedProduct, 1))
        self.assertIsNotNone(self.db.get(ProductMapping, 1))
        self.assertEqual(self.db.query(PendingImport).count(), 0)
        self.assertEqual(self.db.query(AliExpressIdHistory).one().source, "mapping")
        self.assertTrue(self.graphql.call_args.args[0].startswith("query "))

    def test_existing_shopify_product_cannot_be_deleted(self):
        self.graphql.return_value = {"product": {"id": "gid://shopify/Product/501"}}
        self.assertEqual(len(self.run_delete()["skipped"]), 1)
        self.assertIsNotNone(self.db.get(ImportedProduct, 1))

    def test_api_errors_and_incomplete_responses_retain_record(self):
        for response in ({}, {"errors": ["denied"], "product": None}):
            self.graphql.return_value = response
            self.assertFalse(self.run_delete()["deleted"])
        self.graphql.side_effect = RuntimeError("timeout")
        self.assertFalse(self.run_delete()["deleted"])
        self.assertIsNotNone(self.db.get(ImportedProduct, 1))

    def test_active_or_unverifiable_supplier_is_retained(self):
        for response in ({}, {"product_id": "999", "skus": []}, {"product_id": "111", "skus": [], "sale_price": "10"}):
            self.fetch.side_effect = None
            self.fetch.return_value = response
            self.assertFalse(self.run_delete()["deleted"])
        self.graphql.assert_not_called()

    def test_bulk_keeps_active_records_and_supports_mapping_source(self):
        result = self.run_delete([{"source": "imported", "id": 2}, {"source": "mapping", "id": 1}])
        self.assertEqual(result["deleted"], [{"source": "mapping", "id": 1}])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIsNotNone(self.db.get(ImportedProduct, 2))

    def test_remap_during_verification_is_not_deleted(self):
        def changed(*args):
            record = self.db.get(ImportedProduct, 1)
            record.aliexpress_id = "new"
            self.db.commit()
            return {"product": None}
        self.graphql.side_effect = changed
        self.assertFalse(self.run_delete()["deleted"])
        self.assertEqual(self.db.get(ImportedProduct, 1).aliexpress_id, "new")

    def test_shared_supplier_queue_is_preserved(self):
        self.db.add(ProductMapping(id=2, aliexpress_id="111", shopify_product_id="504"))
        self.db.commit()
        self.run_delete()
        self.assertEqual(self.db.query(PendingImport).count(), 1)

    def test_invalid_batch_rejected_before_any_checks(self):
        for items in ([], [{}], [{"source": "imported", "id": True}], [{"source": "imported", "id": i} for i in range(11)]):
            with self.assertRaises(HTTPException):
                delete_dead_listings(self.db, items, self.fetch, self.is_dead)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
