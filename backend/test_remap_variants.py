import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from app import remap_variants as remap
from app.models import ImportedProduct, ProductMapping
from test_variant_price_sync import load_main_functions


class RemapVariantsTests(unittest.TestCase):
    def setUp(self):
        self.raw = {"skus": [{"sku_id": "new1", "label": "black", "sale_price": "12.50", "stock": 0},
                             {"sku_id": "new2", "label": "white", "sale_price": "15", "stock": 8}]}

    def test_replaces_complete_list_without_changing_product_content(self):
        locations = Mock(json=lambda: {"locations": [{"id": 7, "active": True}]})
        response = Mock(json=lambda: {"data": {"productSet": {"product": {"id": "gid://shopify/Product/42"}, "userErrors": []}}})
        with patch.object(remap.shopify, "_shopify_request", side_effect=[locations, response]) as request, \
             patch.object(remap.shopify, "_h", return_value={}), \
             patch.object(remap.shopify, "_graphql", return_value={"metafieldsSet": {"userErrors": []}}), \
             patch.object(remap.shopify, "_invalidate_lock_cache"):
            result = remap.replace_supplier_variants("42", self.raw, "new-product")
        payload = request.call_args.kwargs["json"]["variables"]["input"]
        self.assertEqual(set(payload), {"id", "productOptions", "variants"})
        self.assertEqual(len(payload["variants"]), 2)
        self.assertTrue(all("id" not in variant for variant in payload["variants"]))
        self.assertEqual(payload["variants"][0]["inventoryQuantities"][0]["quantity"], 0)
        self.assertEqual(payload["variants"][1]["metafields"][0]["value"], "new2")
        self.assertFalse(request.call_args.kwargs["retry_ambiguous"])
        self.assertTrue(result["shopify_variants_replaced"])

    def test_invalid_supplier_data_cannot_delete_variants(self):
        for raw in ({"skus": []}, {"skus": [dict(self.raw["skus"][0], stock=None)]},
                    {"skus": [dict(self.raw["skus"][0], sale_price="NaN")]}):
            with patch.object(remap.shopify, "_shopify_request") as request:
                with self.assertRaises(HTTPException):
                    remap.replace_supplier_variants("42", raw, "new")
                request.assert_not_called()

    def test_both_routes_preserve_title_and_allow_same_id_retry(self):
        for mapping in (False, True):
            model = ProductMapping if mapping else ImportedProduct
            record = model(id=1, aliexpress_id="new", shopify_product_id="42")
            record.custom_title = "Merchant title"
            record.custom_description = "Merchant description"
            if not mapping:
                record.original_title = "Original title"
                record.replacement_aliexpress_id = "old"
            db = Mock()
            db.query.return_value.filter.return_value.first.side_effect = [record, None]
            ns = dict(ImportedProduct=ImportedProduct, ProductMapping=ProductMapping,
                      HTTPException=HTTPException, get_product=Mock(return_value=self.raw),
                      is_listing_dead=lambda raw: False, remember_supplier_ids=Mock())
            name = "remap_mapping_listing" if mapping else "remap_listing"
            load_main_functions(ns, name)
            with patch("app.remap_history.validate_remap_target"), patch.object(remap, "replace_supplier_variants", return_value={"shopify_variants_replaced": True}) as replace:
                result = ns[name](1, {"new_aliexpress_id": "new"}, db)
            replace.assert_called_once_with("42", self.raw, "new")
            self.assertEqual(record.custom_title, "Merchant title")
            self.assertEqual(record.custom_description, "Merchant description")
            if not mapping:
                self.assertEqual(record.original_title, "Original title")
                self.assertEqual(record.replacement_aliexpress_id, "old")
            self.assertTrue(result["shopify_variants_replaced"])
            db.commit.assert_called_once()

    def test_rejected_replacement_does_not_commit_new_supplier(self):
        for mapping in (False, True):
            record = (ProductMapping if mapping else ImportedProduct)(id=1, aliexpress_id="old", shopify_product_id="42")
            db = Mock()
            db.query.return_value.filter.return_value.first.side_effect = [record, None]
            ns = dict(ImportedProduct=ImportedProduct, ProductMapping=ProductMapping,
                      HTTPException=HTTPException, get_product=lambda *a: self.raw,
                      is_listing_dead=lambda raw: False, remember_supplier_ids=Mock())
            name = "remap_mapping_listing" if mapping else "remap_listing"
            load_main_functions(ns, name)
            with patch("app.remap_history.validate_remap_target"), patch.object(remap, "replace_supplier_variants", side_effect=HTTPException(502, "Rejected")):
                with self.assertRaises(HTTPException):
                    ns[name](1, {"new_aliexpress_id": "new"}, db)
            self.assertEqual(record.aliexpress_id, "old")
            db.commit.assert_not_called()

    def test_shopify_errors_and_partial_metafield_failure(self):
        locations = Mock(json=lambda: {"locations": [{"id": 7}]})
        for rejected in (True, False):
            body = {"product": {"id": "gid://shopify/Product/42"},
                    "userErrors": [{"message": "Invalid options"}] if rejected else []}
            response = Mock(json=lambda: {"data": {"productSet": body}})
            with patch.object(remap.shopify, "_shopify_request", side_effect=[locations, response]), \
                 patch.object(remap.shopify, "_h", return_value={}), \
                 patch.object(remap.shopify, "_graphql", side_effect=RuntimeError("Unavailable")) as metadata:
                if rejected:
                    with self.assertRaises(HTTPException):
                        remap.replace_supplier_variants("42", self.raw, "new")
                    metadata.assert_not_called()
                else:
                    result = remap.replace_supplier_variants("42", self.raw, "new")
                    self.assertTrue(result["shopify_variants_replaced"])
                    self.assertFalse(result["shopify_metafield_updated"])
                    self.assertTrue(result["warnings"])

    def test_new_sku_images_are_synced_and_failures_reported(self):
        self.raw["skus"][0]["image"] = "https://example.com/black.jpg"
        locations = Mock(json=lambda: {"locations": [{"id": 7}]})
        response = Mock(json=lambda: {"data": {"productSet": {"product": {"id": "gid://shopify/Product/42"}, "userErrors": []}}})
        for outcome in ({"attached": 2, "remaining": 0}, {"attached": 1, "remaining": 1}, RuntimeError("upload failed")):
            with patch.object(remap.shopify, "_shopify_request", side_effect=[locations, response]), \
                 patch.object(remap.shopify, "_h", return_value={}), \
                 patch.object(remap.shopify, "_graphql", return_value={"metafieldsSet": {"userErrors": []}}), \
                 patch.object(remap.shopify, "backfill_sku_images") as images:
                if isinstance(outcome, Exception):
                    images.side_effect = outcome
                else:
                    images.return_value = outcome
                result = remap.replace_supplier_variants("42", self.raw, "new")
            images.assert_called_once_with("42", self.raw["skus"])
            expected = isinstance(outcome, dict) and outcome["remaining"] == 0
            self.assertEqual(result["shopify_images_updated"], expected)
            self.assertEqual(bool(result["warnings"]), not expected)
            self.assertTrue(result["shopify_metafield_updated"])
            self.assertTrue(result["shopify_variants_replaced"])
