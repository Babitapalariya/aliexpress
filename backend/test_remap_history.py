import unittest
import math
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.models import Base, ImportedProduct, ProductMapping
from app.remap_history import history_matches, remember_supplier_ids
from test_variant_price_sync import load_main_functions


class RemapHistoryTests(unittest.TestCase):
    def test_searches_keep_all_ids_and_separate_tables(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            imported = ImportedProduct(aliexpress_id="111111", original_title="Tire", replacement_aliexpress_id="000000")
            mapping = ProductMapping(aliexpress_id="222222", shopify_product_id="555555")
            db.add_all([imported, mapping])
            db.commit()
            for source, record, ids in [("imported", imported, ["333333", "444444"]), ("mapping", mapping, ["666666", "777777"])]:
                for new_id in ids:
                    remember_supplier_ids(db, source, record)
                    record.aliexpress_id = new_id
                    db.commit()
            ns = dict(math=math, ImportedProduct=ImportedProduct, ProductMapping=ProductMapping,
                      history_matches=history_matches, HTTPException=HTTPException)
            load_main_functions(ns, "lookup_product", "list_products", "list_mappings")
            for source, ids in [("imported", ["000000", "111111", "333333", "444444"]), ("mapping", ["222222", "666666", "777777"])]:
                for value in ids:
                    result = ns["lookup_product"](value, source, db)
                    self.assertEqual(result["count"], 1, (source, value))
                    self.assertEqual(result["results"][0]["source"], source)
                    listing = ns["list_products" if source == "imported" else "list_mappings"](1, 20, value, db)
                    self.assertEqual(listing["total"], 1)
            self.assertEqual(ns["lookup_product"]("111111", "mapping", db)["count"], 0)
            self.assertEqual(ns["lookup_product"]("666", "mapping", db)["count"], 1)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()

