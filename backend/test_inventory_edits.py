"""Offline regression checks for stock-only saves and Shopify write failures."""
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
from fastapi import HTTPException
from app import shopify
from app.models import ImportedProduct, ProductMapping
from test_variant_price_sync import load_main_functions


class InventoryEditsTests(TestCase):
    def test_stock_only_save_does_not_change_price_or_mode(self):
        for name in ("update_variant_prices", "update_mapping_variant_prices"):
            record = SimpleNamespace(shopify_product_id="123", price_mode="variant_manual")
            db = Mock()
            db.query.return_value.filter.return_value.first.return_value = record
            writer = Mock(return_value=1)
            ns = {"HTTPException": HTTPException, "ImportedProduct": ImportedProduct,
                  "ProductMapping": ProductMapping, "_set_variant_inventory_levels": writer}
            load_main_functions(ns, name)
            with patch.object(shopify, "save_variant_price_edits") as price_write:
                result = ns[name](1, {"variants": [{"variant_id": "12", "inventory_quantity": 100}]}, db)
                price_write.assert_not_called()
            writer.assert_called_once_with("123", {12: 100})
            self.assertEqual(result["inventory_updated"], 1)
            self.assertEqual(record.price_mode, "variant_manual")
            db.commit.assert_not_called()
            writer.side_effect = HTTPException(502, "Shopify rejected inventory")
            with self.assertRaises(HTTPException):
                ns[name](1, {"variants": [{"variant_id": "12", "inventory_quantity": 0}]}, db)

    def test_invalid_stock_is_rejected(self):
        ns = {"HTTPException": HTTPException}
        load_main_functions(ns, "_parse_inventory_edits")
        for quantity in (-1, 1.5, True, None, "", "1.5"):
            with self.assertRaises(HTTPException):
                ns["_parse_inventory_edits"]([{"variant_id": 1, "inventory_quantity": quantity}])

    def test_push_reports_missing_supplier_link_without_writing_stock(self):
        product = Mock()
        product.json.return_value = {"product": {"variants": [
            {"id": 12, "option1": "Controller A Rear", "inventory_item_id": 42}]}}
        locations = Mock()
        locations.json.return_value = {"locations": [{"id": 7}]}
        metadata = Mock(status_code=200)
        metadata.json.return_value = {"metafields": []}
        with patch.object(shopify.settings, "SHOPIFY_STORE", "test"), \
             patch.object(shopify, "_h", return_value={}), \
             patch.object(shopify.requests, "get", side_effect=[product, locations]), \
             patch.object(shopify, "_shopify_request", return_value=metadata), \
             patch.object(shopify, "get_locked_variant_ids", return_value=set()), \
             patch.object(shopify, "set_variant_inventory_quantities") as writer:
            with self.assertRaises(HTTPException) as error:
                shopify.update_shopify_product_inventory_with_skus("123",
                    [{"sku_id": "a", "label": "Controller A", "stock": 100}], strict=True)
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("controller a rear", error.exception.detail)
            writer.assert_not_called()

    def test_manual_writer_uses_connected_levels_and_confirms_response(self):
        def response(data, status=200):
            return Mock(status_code=status, text="Shopify rejected stock", json=Mock(return_value=data))

        reads = [
            response({"product": {"variants": [{"id": 12, "inventory_item_id": 42, "inventory_management": "shopify"}]}}),
            response({"locations": [{"id": 7, "active": True}, {"id": 8, "legacy": True}]}),
            response({"inventory_levels": [{"location_id": 7, "available": 0}]}),
        ]
        for write_response, fails in [(response({"inventory_level": {"available": 100}}), False),
                                      (response({}, 422), True), (response({}), True)]:
            with patch.object(shopify, "_h", return_value={}), \
                 patch.object(shopify, "_shopify_request", side_effect=reads + [write_response]) as request:
                if fails:
                    with self.assertRaises(HTTPException):
                        shopify.set_variant_inventory_quantities("123", {12: 100})
                else:
                    self.assertEqual(shopify.set_variant_inventory_quantities("123", {12: 100}), 1)
                self.assertEqual(request.call_count, 4)
                self.assertEqual(request.call_args.kwargs["json"],
                                 {"location_id": 7, "inventory_item_id": 42, "available": 100})


if __name__ == "__main__":
    main()
