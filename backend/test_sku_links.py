"""Offline tests for dynamic supplier matching and user-confirmed links."""
from unittest import TestCase, main
from unittest.mock import patch
from decimal import Decimal
from fastapi import HTTPException
from app import shopify, sku_links


class DynamicSupplierLinksTests(TestCase):
    def test_design_aliases_and_nozzle_absence_use_labels_not_order(self):
        skus = [{"sku_id": str(i), "label": label} for i, label in enumerate(
            ["B with nozzle", "A", "A with nozzle", "B"])]
        variants = {i: {"label": label, "ae_sku_id": None} for i, label in enumerate(
            ["Design A  with nozzle", "Design B with nozzle", "Design A No nozzle", "Design B No nozzle"])}
        self.assertEqual({vid: sku["sku_id"] for vid, sku in shopify._match_supplier_variants(variants, skus).items()},
                         {0: "2", 1: "0", 2: "1", 3: "3"})

    def test_aliases_do_not_guess_missing_dimensions_or_conflicts(self):
        for label, suppliers in [
            ("A no nozzle", ["A"]),  # No evidence for the missing modifier.
            ("Design A XL", ["A"]),
            ("Controller A Rear", ["Controller A"]),
            ("Style Red", ["Red", "Design Red"]),
        ]:
            self.assertEqual(shopify._match_supplier_variants({1: {"label": label}},
                [{"sku_id": str(i), "label": value} for i, value in enumerate(suppliers)]), {})
        self.assertEqual(shopify._match_supplier_variants({1: {"label": "Design Red"}, 2: {"label": "Style Red"}},
                         [{"sku_id": "r", "label": "Red"}]), {})

    def test_confirmed_link_wins_over_changed_display_title(self):
        skus = [{"sku_id": "a", "label": "A"}, {"sku_id": "b", "label": "B"}]
        variants = {1: {"label": "B", "ae_sku_id": "a", "sku_link_verified": True},
                    2: {"label": "A", "ae_sku_id": None}}
        self.assertEqual(shopify._match_supplier_variants(variants, skus), {1: skus[0]})

    def test_save_validates_all_links_before_any_write(self):
        state = {1: {"ae_sku_id": None}, 2: {"ae_sku_id": "old"}}
        skus = [{"sku_id": "a"}, {"sku_id": "b"}]
        valid = [{"variant_id": "1", "sku_id": "a", "current_sku_id": None},
                 {"variant_id": "2", "sku_id": "b", "current_sku_id": "old"}]
        invalid = [None, [], valid[:1], [valid[0], valid[0]],
                   [valid[0], {**valid[1], "sku_id": "a"}],
                   [valid[0], {**valid[1], "sku_id": "foreign"}],
                   [valid[0], {**valid[1], "current_sku_id": None}]]
        with patch.object(shopify, "get_variant_sync_state", return_value=state), \
             patch.object(shopify, "bulk_update_variant_prices", return_value={"success": True}) as write:
            for links in invalid:
                with self.assertRaises(HTTPException):
                    sku_links.save_links("any-product", skus, links)
            write.assert_not_called()
            self.assertEqual(sku_links.save_links("any-product", skus, valid)["updated"], 2)
            updates = write.call_args.args[1]
            self.assertTrue(all(row["sku_link_verified"] for row in updates))
            self.assertTrue(all("price" not in row and "price_increase" not in row for row in updates))
            self.assertTrue(all(row["supplier_history"] is None for row in updates))

    def test_shopify_confirms_manual_link_flag(self):
        def graphql(query, variables):
            row = variables["variants"][0]
            self.assertIn({"namespace": "aliexpress", "key": "sku_link_verified",
                           "type": "boolean", "value": "true"}, row["metafields"])
            return {"productVariantsBulkUpdate": {"userErrors": [], "productVariants": [
                {"id": row["id"], "aeSku": {"value": "a"}, "skuVerified": {"value": "true"}}]}}
        with patch.object(shopify, "_graphql", side_effect=graphql):
            self.assertTrue(shopify.bulk_update_variant_prices("123", [
                {"variant_id": 1, "ae_sku_id": "a", "sku_link_verified": True}])["success"])

    def test_price_sync_resolves_alias_and_preserves_saved_adjustment(self):
        state = {1: {"label": "Design Blue", "ae_sku_id": None, "price": "10.00",
                     "price_increase": Decimal("-2"), "locks": {"price": False}}}
        with patch.object(shopify.settings, "SHOPIFY_STORE", "test"), \
             patch.object(shopify, "get_variant_sync_state", return_value=state), \
             patch.object(shopify, "bulk_update_variant_prices", return_value={"success": True, "updated": 1, "errors": []}) as write:
            result = shopify.update_shopify_product_prices_with_skus("any-product",
                [{"sku_id": "blue", "label": "Blue", "sale_price": "20"}])
            self.assertEqual(result, "updated")
            update = write.call_args.args[1][0]
            self.assertEqual(update["ae_sku_id"], "blue")
            self.assertEqual(update["price"], "18.00")
            self.assertNotIn("price_increase", update)


if __name__ == "__main__":
    main()
