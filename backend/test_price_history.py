"""Supplier movement display stays separate from per-check comparisons."""
import unittest
from app.price_history import observe_supplier_price, supplier_change_summary


class SupplierPriceHistoryTests(unittest.TestCase):
    def test_increase_survives_unchanged_checks_then_decrease_replaces_it(self):
        history = observe_supplier_price(None, "20")
        self.assertIsNone(supplier_change_summary(history)["supplier_price_change"])
        history = observe_supplier_price(history, "25")
        moved_at = history["last_changed_at"]
        for _ in range(3):
            history = observe_supplier_price(history, "25")
            self.assertEqual(history["change"], "0.00")
            self.assertEqual(supplier_change_summary(history), {
                "supplier_price_change": "5.00", "supplier_price_changed_at": moved_at,
                "supplier_previous_price": "20.00", "supplier_current_price": "25.00"})
        history = observe_supplier_price(history, "23")
        self.assertEqual(history["last_change"], "-2.00")
        self.assertEqual(history["last_from"], "25.00")
        self.assertEqual(history["last_to"], "23.00")
        self.assertEqual(observe_supplier_price(history, "23")["last_change"], "-2.00")

    def test_legacy_nonzero_movement_is_preserved(self):
        history = observe_supplier_price({"price": "52.92", "change": "2.56", "changed_at": "old-date"}, "52.92")
        self.assertEqual(history["last_change"], "2.56")
        self.assertEqual(history["last_from"], "50.36")
        self.assertEqual(history["last_to"], "52.92")
        self.assertEqual(history["last_changed_at"], "old-date")

    def test_missing_old_history_is_not_invented(self):
        history = observe_supplier_price({"price": "10", "change": "0.00", "changed_at": None}, "10")
        self.assertEqual(history["last_change"], "0.00")
        self.assertIsNone(history["last_from"])
        self.assertIsNone(history["last_changed_at"])


if __name__ == "__main__":
    unittest.main()
