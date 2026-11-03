"""Verify complete variant payloads and Edit forms without live API writes."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import HTTPException
from app import shopify
from app.models import ImportedProduct, ProductMapping
from test_variant_price_sync import load_main_functions


class LargeVariantTests(unittest.TestCase):
    def test_import_payload_preserves_150_variants_including_zero_stock(self):
        skus = [{"sku_id": str(i), "label": f"Style {i}", "sale_price": str(i), "stock": i % 3}
                for i in range(1, 151)]
        payload = shopify.normalize_aliexpress_product({"skus": skus})
        self.assertEqual(len(payload["variants"]), 150)
        self.assertEqual([v["metafields"][0]["value"] for v in payload["variants"]], [s["sku_id"] for s in skus])
        self.assertEqual(payload["variants"][-1]["inventory_quantity"], 0)

    def test_both_edit_forms_include_second_page_with_price_stock_and_markup(self):
        def page(first, last, more):
            nodes = [{"node": {"id": f"gid://shopify/ProductVariant/{i}", "price": str(i),
                      "sku": f"SKU-{i}", "inventoryQuantity": i % 3, "compareAtPrice": "200",
                      "selectedOptions": [{"name": "Style", "value": f"Style {i}"}],
                      "increase": {"value": "5"}}} for i in range(first, last + 1)]
            return {"product": {"title": "Large product", "variants": {"edges": nodes,
                    "pageInfo": {"hasNextPage": more, "endCursor": str(last)}}}}
        for name in ("get_product_variants", "get_mapping_variants"):
            db = Mock()
            db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(shopify_product_id="123", price_increase=2)
            ns = {"ImportedProduct": ImportedProduct, "ProductMapping": ProductMapping, "HTTPException": HTTPException}
            load_main_functions(ns, name)
            with patch.object(shopify, "_graphql", side_effect=[page(1, 100, True), page(101, 150, False)]) as query:
                result = ns[name](1, db)
            self.assertEqual(result["title"], "Large product")
            self.assertEqual(len(result["variants"]), 150)
            last = result["variants"][-1]
            self.assertEqual((last["variant_id"], last["sku"], last["price"], last["inventory_quantity"]), (150, "SKU-150", "150", 0))
            self.assertEqual(last["price_increase"], 5)
            self.assertEqual(last["product_price_increase"], 2)
            self.assertEqual(query.call_args_list[1].args[1]["after"], "100")


if __name__ == "__main__":
    unittest.main()
