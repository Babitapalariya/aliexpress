"""Offline regression tests: no database, scheduler or live Shopify writes."""
import ast
import contextlib
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from app import shopify
from app.models import ImportedProduct, ProductMapping

REAL_BULK_UPDATE = shopify.bulk_update_variant_prices


def load_main_functions(namespace, *names):
    # main.py starts DB setup on import. Load the actual function bodies without
    # executing startup code, and inject the database/external-service doubles.
    tree = ast.parse(Path(__file__).with_name("app").joinpath("main.py").read_text(encoding="utf-8"))
    functions = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            node.returns = None
            node.args.defaults = []
            for arg in node.args.args:
                arg.annotation = None
            functions.append(node)
    namespace["__package__"] = "app"
    exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), namespace)


class VariantPriceSyncTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [
            {"id": f"gid://shopify/ProductVariant/{i}", "price": "10.00",
             "selectedOptions": [{"name": "Color", "value": f"Color {i}"}],
             "aeSku": {"value": f"ae-{i}"}, "increase": None,
             "priceLock": None, "inventoryLock": None, "imageLock": None}
            for i in (1, 2, 3, 4)
        ]
        self.skus = [{"sku_id": f"ae-{i}", "sale_price": "12", "label": f"Color {i}"}
                     for i in (4, 2, 1, 3)]  # Supplier order differs from Shopify.
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(shopify.settings, "SHOPIFY_STORE", "test"))
        self.query = self.stack.enter_context(patch.object(shopify, "_graphql", side_effect=self.graphql))
        self.bulk = self.stack.enter_context(patch.object(shopify, "bulk_update_variant_prices", side_effect=self.update))
        self.stack.enter_context(patch.object(shopify, "set_variant_price_increase", side_effect=self.increase))
        self.set_lock = self.stack.enter_context(patch.object(shopify, "set_variant_lock", side_effect=self.lock))
        shopify._invalidate_lock_cache("123")

    def graphql(self, query, variables):
        return {"product": {"variants": {"edges": [{"node": copy.deepcopy(n)} for n in self.nodes],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

    def update(self, product_id, updates):
        for row in updates:
            if "price" in row:
                self.nodes[row["variant_id"] - 1]["price"] = row["price"]
            if "price_increase" in row:
                self.increase(row["variant_id"], row["price_increase"])
            if "ae_sku_id" in row:
                self.nodes[row["variant_id"] - 1]["aeSku"] = {"value": row["ae_sku_id"]}
            if "supplier_history" in row:
                self.nodes[row["variant_id"] - 1]["supplierHistory"] = {
                    "id": f"gid://shopify/Metafield/{row['variant_id']}",
                    "value": json.dumps(row["supplier_history"]),
                }
        return {"success": True, "updated": len(updates), "errors": []}

    def increase(self, vid, amount):
        self.nodes[vid - 1]["increase"] = {"value": str(amount)}
        return True

    def lock(self, vid, field, value):
        self.nodes[vid - 1][field + "Lock"] = {"value": str(value).lower()}
        return True

    def prices(self):
        return [float(node["price"]) for node in self.nodes]

    def sync(self):
        return shopify.update_shopify_product_prices_with_skus("123", self.skus)

    def test_edit_save_mixed_locks_and_scheduled_supplier_updates(self):
        product = SimpleNamespace(id=1, shopify_product_id="123", price_mode="auto",
                                  price_increase=0, custom_price=None, track_price=True,
                                  aliexpress_id="ae-product", original_price="10")
        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = product
        namespace = {"ImportedProduct": ImportedProduct, "HTTPException": HTTPException,
                     "get_product": Mock(side_effect=lambda *_: {"skus": copy.deepcopy(self.skus)}),
                     "is_listing_dead": lambda _: False,
                     "update_shopify_product_prices_with_skus": shopify.update_shopify_product_prices_with_skus}
        load_main_functions(namespace, "update_variant_prices", "_resume_variant_price_sync", "sync_product_price")
        # Increase each variant, lock two before clicking Save Changes.
        self.lock(2, "price", True)
        self.lock(4, "price", True)
        namespace["update_variant_prices"](1, {"variants": [
            {"variant_id": i, "price": 10 + amount, "price_increase": amount}
            for i, amount in ((1, 2), (2, 5), (3, 8), (4, 3))
        ]}, db)
        self.set_lock.assert_not_called()  # Saving a markup must not change locks.
        self.assertEqual(product.price_mode, "variant_manual")
        self.assertEqual(self.prices(), [12, 15, 18, 13])
        with patch.object(shopify, "update_shopify_product_inventory_with_skus", return_value=True):
            self.assertEqual(namespace["sync_product_price"](1, db), "updated")
            self.assertEqual(self.prices(), [14, 15, 20, 13])
            self.assertEqual(namespace["sync_product_price"](1, db), "unchanged")
            self.lock(2, "price", False)
            for sku in self.skus:
                sku["sale_price"] = "9"
            namespace["sync_product_price"](1, db)
        self.assertEqual(self.prices(), [11, 14, 17, 13])
        self.assertEqual([float(n["increase"]["value"]) for n in self.nodes], [2, 5, 8, 3])

    def test_one_cent_and_repeated_sync(self):
        self.increase(1, 3)
        self.nodes[0]["price"] = "15.01"
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices()[0], 15)
        self.assertEqual(self.sync(), "unchanged")

    def test_fresh_locks_override_old_cache(self):
        shopify._lock_cache["123"] = (float("inf"), {i: {"price": True} for i in (1, 2, 3, 4)})
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices(), [12, 12, 12, 12])

    def test_failed_metadata_read_makes_no_price_writes(self):
        self.query.side_effect = RuntimeError("Shopify unavailable")
        self.assertEqual(self.sync(), "failed")
        self.bulk.assert_not_called()

    def test_all_locked_is_unchanged(self):
        for i in (1, 2, 3, 4):
            self.lock(i, "price", True)
        self.assertEqual(self.sync(), "unchanged")
        self.assertTrue(all("price" not in row for call in self.bulk.call_args_list for row in call.args[1]))

    def test_inventory_lock_does_not_block_price(self):
        self.lock(1, "inventory", True)
        self.lock(2, "image", True)
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices(), [12, 12, 12, 12])

    def test_missing_sku_does_not_take_sibling_price(self):
        self.nodes[0]["aeSku"]["value"] = "removed"
        self.nodes[0]["selectedOptions"][0]["value"] = "Removed option"
        self.assertEqual(self.sync(), "failed")
        self.assertEqual(self.prices(), [10, 12, 12, 12])

    def controller_fixture(self):
        supplier = [
            ("12000053381971912", "48-72V50A", "130.68", 104),
            ("12000053381971913", "48-72V70A", "186.99", 109),
            ("12000053381971914", "48-72V90A", "243.86", 111),
            ("12000053381971915", "48-96V120A", "270.61", 110),
            ("12000053381971916", "48-96V160A", "327.37", 107),
            ("12000053381971911", "72v lcd", "52.92", 110),
        ]
        self.skus = [dict(sku_id=sid, label=label, sale_price=price, stock=stock)
                     for sid, label, price, stock in supplier]
        template = copy.deepcopy(self.nodes[0])
        self.nodes = []
        for i, (label, increase, price) in enumerate([
            ("48–72V70A", 25, "211.99"), ("48–72V50A", 25, "155.68"),
            ("48–96V160A", 40, "367.37"), ("72VLCD", 10, "65.48"),
            ("48–72V90A", 30, "273.86"), ("48–96V120A", 30, "300.61"),
        ], 1):
            node = copy.deepcopy(template)
            node.update(id=f"gid://shopify/ProductVariant/{i}", price=price,
                        selectedOptions=[{"name": "Variant", "value": label}],
                        aeSku={"value": f"old-{i}"}, increase={"value": str(increase)})
            self.nodes.append(node)

    def test_controller_stale_ids_repaired_with_individual_markups(self):
        self.controller_fixture()
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices(), [211.99, 155.68, 367.37, 62.92, 273.86, 300.61])
        self.assertEqual([n["aeSku"]["value"] for n in self.nodes],
                         [self.skus[i]["sku_id"] for i in (1, 0, 4, 5, 2, 3)])
        self.assertEqual(self.sync(), "unchanged")
        self.skus[5]["sale_price"] = "50.00"
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices()[3], 60)
        self.assertEqual(shopify.get_variant_sync_state("123")[4]["supplier_history"]["change"], "-2.92")

    def test_controller_wrong_existing_ids_repaired_without_overriding_locks(self):
        self.controller_fixture()
        for node, sku in zip(self.nodes, self.skus):
            node["aeSku"] = {"value": sku["sku_id"]}
        self.lock(4, "price", True)
        self.sync()
        self.assertEqual(self.prices()[3], 65.48)
        self.assertEqual(self.nodes[3]["aeSku"]["value"], self.skus[5]["sku_id"])
        self.lock(4, "price", False)
        self.sync()
        self.assertEqual(self.prices()[3], 62.92)

    def test_lcd_old_change_clears_and_future_prices_follow_both_directions(self):
        self.controller_fixture()
        shopify.store_aliexpress_sku_ids("123", self.skus)
        self.nodes[3]["supplierHistory"] = {"id": "gid://shopify/Metafield/4", "value": json.dumps({
            "price": "52.92", "change": "2.56", "changed_at": "2026-09-01T00:00:00+00:00"})}
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices()[3], 62.92)
        self.assertEqual(shopify.get_variant_sync_state("123")[4]["supplier_history"]["change"], "0.00")
        for supplier, expected, change in [("55.48", 65.48, "2.56"), ("50.00", 60, "-5.48"), ("50.00", 60, "0.00")]:
            self.skus[5]["sale_price"] = supplier
            self.sync()
            state = shopify.get_variant_sync_state("123")[4]
            self.assertEqual(self.prices()[3], expected)
            self.assertEqual(state["supplier_history"]["change"], change)
            self.assertEqual(float(state["price_increase"]), 10)

    def test_repaired_link_does_not_report_wrong_variant_history_as_movement(self):
        self.controller_fixture()
        self.nodes[3]["supplierHistory"] = {"id": "gid://shopify/Metafield/4", "value": json.dumps({
            "price": "55.48", "change": "2.56", "changed_at": "2026-09-01T00:00:00+00:00"})}
        self.sync()
        history = shopify.get_variant_sync_state("123")[4]["supplier_history"]
        self.assertEqual(history["price"], "52.92")
        self.assertIsNone(history["change"])
        self.assertIsNone(history["changed_at"])
        self.assertEqual(self.prices()[3], 62.92)

    def test_backfill_matches_labels_instead_of_position(self):
        self.controller_fixture()
        shopify.store_aliexpress_sku_ids("123", self.skus)
        self.assertEqual(self.nodes[0]["aeSku"]["value"], self.skus[1]["sku_id"])
        self.assertEqual(self.prices()[3], 65.48)  # Link-only backfill.

    def test_ambiguous_labels_and_duplicate_claims_are_not_guessed(self):
        variants = {1: {"label": "Red", "ae_sku_id": "old"}}
        skus = [{"sku_id": "a", "label": "Red"}, {"sku_id": "b", "label": "red"}]
        self.assertEqual(shopify._match_supplier_variants(variants, skus), {})
        variants[1]["ae_sku_id"] = "b"
        self.assertEqual(shopify._match_supplier_variants(variants, skus), {1: skus[1]})
        variants[2] = dict(variants[1])
        self.assertEqual(shopify._match_supplier_variants(variants, skus), {})

    def test_legacy_import_options_and_custom_titles(self):
        skus = [{"sku_id": "a", "label": "72v lcd", "sku_attr": "14:29#72v lcd"}]
        for label, sid in [("29#72v lcd", None), ("Custom display", "a")]:
            self.assertEqual(shopify._match_supplier_variants({1: {"label": label, "ae_sku_id": sid}}, skus), {1: skus[0]})

    def test_bulk_verifies_repaired_sku_link(self):
        def mutation(query, variables):
            row = variables["variants"][0]
            self.assertIn({"namespace": "aliexpress", "key": "sku_id",
                           "type": "single_line_text_field", "value": "new"}, row["metafields"])
            return {"productVariantsBulkUpdate": {"userErrors": [], "productVariants": [
                {"id": row["id"], "aeSku": {"value": "new"}}]}}
        self.query.side_effect = mutation
        self.assertTrue(REAL_BULK_UPDATE("123", [{"variant_id": 1, "ae_sku_id": "new"}])["success"])
        self.query.side_effect = None
        self.query.return_value = {"productVariantsBulkUpdate": {"userErrors": [], "productVariants": [
            {"id": "gid://shopify/ProductVariant/1", "aeSku": None}]}}
        self.assertFalse(REAL_BULK_UPDATE("123", [{"variant_id": 1, "ae_sku_id": "new"}])["success"])

    def test_inventory_reordered_labels_and_missing_match(self):
        self.controller_fixture()
        variants = [{"id": i, "option1": node["selectedOptions"][0]["value"],
                     "inventory_item_id": i, "inventory_quantity": 0}
                    for i, node in enumerate(self.nodes, 1)]
        variants.insert(0, {"id": 7, "option1": "Unknown", "inventory_item_id": 7, "inventory_quantity": 5})
        product, locations, metadata = Mock(), Mock(), Mock(status_code=200)
        product.json.return_value = {"product": {"variants": variants}}
        locations.json.return_value = {"locations": [{"id": 99}]}
        metadata.json.return_value = {"metafields": [{"value": "old"}]}
        with patch.object(shopify, "_h", return_value={}), \
             patch.object(shopify.requests, "get", side_effect=[product, locations]), \
             patch.object(shopify, "_shopify_request", return_value=metadata), \
             patch.object(shopify, "get_locked_variant_ids", return_value={3}), \
             patch.object(shopify.requests, "post", return_value=Mock()) as write:
            self.assertTrue(shopify.update_shopify_product_inventory_with_skus("123", self.skus))
        self.assertEqual({c.kwargs["json"]["inventory_item_id"]: c.kwargs["json"]["available"]
                          for c in write.call_args_list}, {1: 109, 2: 104, 4: 110, 5: 111, 6: 110})

    def test_paginated_rules_include_later_variants(self):
        def page(query, variables):
            second = variables["after"] == "next"
            nodes = self.nodes[2:] if second else self.nodes[:2]
            return {"product": {"variants": {"edges": [{"node": n} for n in nodes],
                    "pageInfo": {"hasNextPage": not second, "endCursor": "next"}}}}
        self.query.side_effect = page
        self.increase(3, 8)
        self.lock(4, "price", True)
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices(), [12, 12, 20, 10])

    def test_legacy_product_manual_mode_resumes(self):
        product = SimpleNamespace(price_mode="manual", shopify_product_id="123", custom_price="15")
        self.lock(2, "price", True)
        namespace = {}
        load_main_functions(namespace, "_resume_variant_price_sync")
        self.assertTrue(namespace["_resume_variant_price_sync"](product, Mock()))
        self.assertEqual(product.price_mode, "variant_manual")
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices(), [12, 10, 12, 12])

    def test_large_product_updates_all_unlocked_variants_in_batches(self):
        template = copy.deepcopy(self.nodes[0])
        self.nodes = []
        self.skus = []
        for i in range(1, 206):
            node = copy.deepcopy(template)
            node["id"] = f"gid://shopify/ProductVariant/{i}"
            node["aeSku"] = {"value": str(i)}
            node["increase"] = {"value": str(i)}
            self.nodes.append(node)
            self.skus.append({"sku_id": str(i), "sale_price": "12"})
        self.lock(2, "price", True)
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices()[1], 10)
        self.assertEqual(self.prices()[-1], 217)
        self.assertEqual([len(call.args[1]) for call in self.bulk.call_args_list], [100, 100, 5])

    def test_supplier_movements_stay_separate_from_manual_increases(self):
        self.increase(1, 3)
        self.lock(2, "price", True)
        self.sync()  # First observation: supplier price 12.
        state = shopify.get_variant_sync_state("123")
        self.assertIsNone(state[1]["supplier_history"]["change"])
        for sku in self.skus:
            sku["sale_price"] = "14"
        self.sync()
        state = shopify.get_variant_sync_state("123")
        self.assertEqual(state[1]["supplier_history"]["change"], "2.00")
        self.assertEqual(float(state[1]["price_increase"]), 3)
        self.assertEqual(self.prices()[:2], [17, 10])
        self.assertEqual(state[2]["supplier_history"]["change"], "2.00")
        for sku in self.skus:
            sku["sale_price"] = "9"
        self.sync()
        history = shopify.get_variant_sync_state("123")[1]["supplier_history"]
        self.assertEqual(history["change"], "-5.00")
        self.assertEqual(self.prices()[:2], [12, 10])
        self.sync()
        latest = shopify.get_variant_sync_state("123")[1]["supplier_history"]
        self.assertEqual(latest["change"], "0.00")
        self.assertIsNone(latest["changed_at"])
        self.bulk.reset_mock()
        self.assertEqual(self.sync(), "unchanged")
        self.bulk.assert_not_called()

    def test_product_wide_markup_is_not_recorded_as_supplier_movement(self):
        for sku in self.skus:
            sku["_supplier_price"] = "10"
        self.sync()
        for sku in self.skus:
            sku["sale_price"] = "20"  # Product-wide markup changed; supplier did not.
        self.sync()
        self.assertEqual(shopify.get_variant_sync_state("123")[1]["supplier_history"]["change"], "0.00")

    def test_partial_shopify_failure_is_reported(self):
        self.bulk.side_effect = None
        self.bulk.return_value = {"success": True, "updated": 1, "errors": ["Variant update failed"]}
        self.assertEqual(self.sync(), "failed")

    def test_absolute_edits_preserve_each_increase_on_future_sync(self):
        shopify.save_variant_price_edits("123", [
            {"variant_id": 1, "price": "12"},
            {"variant_id": 2, "price": "15"},
            {"variant_id": 3, "price": "18"},
        ])
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices(), [14, 17, 20, 12])
        # Another edit to A adds to A's stored adjustment, not B's or C's.
        shopify.save_variant_price_edits("123", [{"variant_id": 1, "price": "17"}])
        self.assertEqual(self.sync(), "unchanged")
        self.assertEqual(self.prices(), [17, 17, 20, 12])

    def test_locked_final_price_edit_preserves_markup_and_supplier_sync(self):
        self.increase(1, 5)
        self.nodes[0]["price"] = "124.47"
        self.lock(1, "price", True)
        shopify.save_variant_price_edits("123", [{"variant_id": 1, "price": "24.47"}])
        self.assertEqual(float(self.nodes[0]["increase"]["value"]), 5)
        for supplier in ("119.47", "19.47", "15.00"):
            for sku in self.skus:
                sku["sale_price"] = supplier
            self.sync()
            self.assertEqual(self.prices()[0], 24.47)
            self.assertEqual(float(self.nodes[0]["increase"]["value"]), 5)
            self.assertEqual(self.prices()[1], float(supplier))
        self.lock(1, "price", False)
        self.sync()
        self.assertEqual(self.prices()[0], 20)

    def test_explicit_markup_repair_keeps_locked_price(self):
        self.increase(1, -95)
        self.nodes[0]["price"] = "24.47"
        self.lock(1, "price", True)
        shopify.save_variant_price_edits("123", [{"variant_id": 1, "price": "24.47", "price_increase": "5"}])
        self.assertEqual(self.prices()[0], 24.47)
        self.assertEqual(float(self.nodes[0]["increase"]["value"]), 5)
        self.assertEqual(self.nodes[0]["priceLock"]["value"], "true")

    def test_rejects_invalid_increase_before_any_prices_are_written(self):
        with self.assertRaises(HTTPException) as error:
            shopify.save_variant_price_edits("123", [
                {"variant_id": 1, "price": "12", "price_increase": "2"},
                {"variant_id": 2, "price": "15", "price_increase": "NaN"},
            ])
        self.assertEqual(error.exception.status_code, 400)
        self.bulk.assert_not_called()

    def test_graphql_saves_price_and_correct_markup_together(self):
        def mutation(query, variables):
            self.assertIn("allowPartialUpdates: false", query)
            rows = variables["variants"]
            self.assertEqual([r["price"] for r in rows], ["12.00", "15.00", "18.00"])
            self.assertEqual([r["metafields"][0]["value"] for r in rows], ["2.00", "5.00", "8.00"])
            self.assertEqual(rows[0]["metafields"][0]["id"], "gid://shopify/Metafield/9")
            self.assertEqual(rows[1]["metafields"][0]["key"], "price_increase")
            return {"productVariantsBulkUpdate": {"userErrors": [], "productVariants": [
                {"id": r["id"], "price": r["price"], "priceIncrease": {"value": r["metafields"][0]["value"]}}
                for r in rows
            ]}}
        self.query.side_effect = mutation
        result = REAL_BULK_UPDATE("123", [
            {"variant_id": 1, "price": "12.00", "price_increase": "2.00", "increase_metafield_id": "gid://shopify/Metafield/9"},
            {"variant_id": 2, "price": "15.00", "price_increase": "5.00"},
            {"variant_id": 3, "price": "18.00", "price_increase": "8.00"},
        ])
        self.assertTrue(result["success"])

    def test_missing_saved_increase_is_not_reported_as_success(self):
        self.query.side_effect = None
        self.query.return_value = {"productVariantsBulkUpdate": {"userErrors": [], "productVariants": [
            {"id": "gid://shopify/ProductVariant/1", "price": "12.00", "priceIncrease": None}
        ]}}
        result = REAL_BULK_UPDATE("123", [{"variant_id": 1, "price": "12.00", "price_increase": "2.00"}])
        self.assertFalse(result["success"])

    def mapping_context(self, mode="auto", increase=0):
        mapping = SimpleNamespace(id=1, shopify_product_id="123", price_mode=mode,
                                  price_increase=increase, track_price=True,
                                  aliexpress_id="mapped-ae", is_dead_listing=False)
        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = mapping
        db.query.return_value.filter.return_value.all.return_value = [mapping]
        namespace = {"ProductMapping": ProductMapping, "HTTPException": HTTPException,
                     "get_latest_token": Mock(),
                     "get_product": Mock(side_effect=lambda *_: {"skus": copy.deepcopy(self.skus)}),
                     "is_listing_dead": lambda _: False,
                     "update_shopify_product_prices_with_skus": shopify.update_shopify_product_prices_with_skus}
        load_main_functions(namespace, "update_mapping_variant_prices", "_resume_variant_price_sync",
                            "sync_mapped_product_price", "sync_all_mapped_products_background",
                            "delete_mapping_variant")
        return mapping, db, namespace

    def test_mapping_edits_manual_and_hourly_sync_preserve_rules_and_history(self):
        mapping, db, ns = self.mapping_context()
        self.lock(2, "price", True)
        ns["update_mapping_variant_prices"](1, {"variants": [
            {"variant_id": i, "price": 10 + amount, "price_increase": amount}
            for i, amount in ((1, 2), (2, 5), (3, 8))
        ]}, db)
        self.assertEqual(mapping.price_mode, "variant_manual")
        self.set_lock.assert_not_called()
        with patch.object(shopify, "update_shopify_product_inventory_with_skus", return_value=True), \
             patch("app.database.SessionLocal", return_value=db):
            ns["sync_mapped_product_price"]("mapped-ae", db)
            self.assertEqual(self.prices(), [14, 15, 20, 12])
            for sku in self.skus:
                sku["sale_price"] = "9"
            ns["sync_all_mapped_products_background"]()
        self.assertEqual(self.prices(), [11, 15, 17, 9])
        self.assertEqual(shopify.get_variant_sync_state("123")[1]["supplier_history"]["change"], "-3.00")
        self.assertEqual(float(shopify.get_variant_sync_state("123")[1]["price_increase"]), 2)

    def test_mapping_manual_sync_preserves_existing_product_wide_increase(self):
        mapping, db, ns = self.mapping_context("increase", 4)
        self.increase(1, 3)
        with patch.object(shopify, "update_shopify_product_inventory_with_skus", return_value=True):
            ns["sync_mapped_product_price"]("mapped-ae", db)
        self.assertEqual(self.prices(), [19, 16, 16, 16])
        self.assertEqual(mapping.price_increase, 4)
        self.assertEqual(mapping.price_mode, "increase")
        self.assertEqual(shopify.get_variant_sync_state("123")[1]["supplier_history"]["price"], "12.00")

    def test_screenshot_mixed_locks_keep_each_manual_increase(self):
        for i, (price, increase) in enumerate(zip([110, 50, 80, 60], [10, 20, 30, 40]), 1):
            self.nodes[i - 1]["price"] = str(price)
            self.increase(i, increase)
        self.lock(1, "price", True)
        self.lock(2, "price", True)
        self.skus = [{"sku_id": f"ae-{i}", "sale_price": str(price)}
                     for i, price in enumerate([120, 60, 90, 70], 1)]
        self.assertEqual(self.sync(), "updated")
        self.assertEqual(self.prices(), [110, 50, 120, 110])
        self.assertEqual(self.sync(), "unchanged")

    def test_mapping_mixed_locks_do_not_disable_saved_product_adjustment(self):
        mapping, db, ns = self.mapping_context("variant_manual", 10)
        self.increase(3, 20)
        self.increase(4, 30)
        self.lock(1, "price", True)
        self.lock(2, "price", True)
        self.nodes[0]["price"] = "110"
        self.nodes[1]["price"] = "50"
        self.skus = [{"sku_id": f"ae-{i}", "sale_price": str(price)}
                     for i, price in enumerate([120, 60, 90, 70], 1)]
        with patch.object(shopify, "update_shopify_product_inventory_with_skus", return_value=True):
            ns["sync_mapped_product_price"]("mapped-ae", db)
        self.assertEqual(self.prices(), [110, 50, 120, 110])
        self.assertEqual(mapping.price_increase, 10)

    def test_imported_mixed_locks_preserve_adjustment_in_manual_and_hourly_sync(self):
        product, db, ns = self.mapping_context("variant_manual", 10)
        product.original_price = "50"
        product.custom_price = None
        ns["ImportedProduct"] = ImportedProduct
        load_main_functions(ns, "sync_product_price", "manual_product_price_sync")
        self.increase(3, 20)
        self.increase(4, 30)
        self.lock(1, "price", True)
        self.lock(2, "price", True)
        self.nodes[0]["price"] = "110"
        self.nodes[1]["price"] = "50"
        self.skus = [{"sku_id": f"ae-{i}", "sale_price": str(price)}
                     for i, price in enumerate([120, 60, 90, 70], 1)]
        with patch.object(shopify, "update_shopify_product_inventory_with_skus", return_value=True):
            ns["sync_product_price"](1, db)
            self.assertEqual(self.prices(), [110, 50, 120, 110])
            ns["manual_product_price_sync"](1, db)
            self.assertEqual(product.price_increase, 10)
            self.assertEqual(self.prices(), [110, 50, 120, 110])

    def test_mapping_hourly_sync_retains_adjustments_in_mixed_lock_mode(self):
        mapping, db, ns = self.mapping_context("variant_manual", 10)
        self.increase(3, 20)
        self.lock(1, "price", True)
        with patch("app.database.SessionLocal", return_value=db), \
             patch.object(shopify, "update_shopify_product_inventory_with_skus", return_value=True):
            ns["sync_all_mapped_products_background"]()
        self.assertEqual(self.prices(), [10, 22, 42, 22])
        self.assertEqual(mapping.price_increase, 10)

    def test_editing_one_variant_retains_shared_increase_for_unlocked_siblings(self):
        for imported in (False, True):
            with self.subTest(imported=imported):
                record, db, ns = self.mapping_context("increase", 95)
                record.custom_price = None
                record.original_price = "10"
                ns["ImportedProduct"] = ImportedProduct
                load_main_functions(ns, "update_variant_prices", "sync_product_price")
                for node in self.nodes:
                    node["price"] = "105"
                save = ns["update_variant_prices" if imported else "update_mapping_variant_prices"]
                save(1, {"variants": [{"variant_id": 1, "price": 10, "price_increase": -95}]}, db)
                self.assertEqual(record.price_increase, 95)
                self.lock(1, "price", True)
                record.price_mode = "variant_manual"
                with patch.object(shopify, "update_shopify_product_inventory_with_skus", return_value=True):
                    if imported:
                        ns["sync_product_price"](1, db)
                    else:
                        ns["sync_mapped_product_price"]("mapped-ae", db)
                self.assertEqual(self.prices(), [10, 107, 107, 107])

    def test_mapping_delete_verifies_ownership_and_protects_final_variant(self):
        _, db, ns = self.mapping_context()
        response = Mock(status_code=200)
        response.json.return_value = {"product": {"variants": [{"id": 1}, {"id": 2}]}}
        with patch.object(shopify, "_h", return_value={}), \
             patch.object(shopify, "_shopify_request", return_value=response) as request:
            self.assertEqual(ns["delete_mapping_variant"](1, 2, db)["variant_id"], 2)
            self.assertEqual(request.call_args.args[0], "DELETE")
            request.reset_mock()
            with self.assertRaises(HTTPException):
                ns["delete_mapping_variant"](1, 99, db)
            self.assertEqual(request.call_count, 1)
            response.json.return_value = {"product": {"variants": [{"id": 1}]}}
            with self.assertRaises(HTTPException):
                ns["delete_mapping_variant"](1, 1, db)


if __name__ == "__main__":
    unittest.main()
