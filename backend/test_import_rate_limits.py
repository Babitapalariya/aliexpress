"""Offline import retry tests; never create real Shopify products."""
import contextlib
import json
import unittest
from unittest.mock import patch
import requests
from fastapi import HTTPException
from app import shopify


def response(status, body=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body or {}).encode()
    result.url = "https://test.myshopify.com/admin/api/products.json"
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


class ImportRateLimitTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(shopify.settings, "SHOPIFY_STORE", "test"))
        stack.enter_context(patch.object(shopify, "_h", return_value={}))
        self.throttle = stack.enter_context(patch.object(shopify, "_throttle"))
        self.sleep = stack.enter_context(patch.object(shopify.time, "sleep"))
        self.exists = stack.enter_context(patch.object(shopify, "check_product_exists_in_shopify", return_value=False))
        self.request = stack.enter_context(patch.object(shopify.requests, "request"))

    def test_create_recovers_from_429_and_respects_full_wait(self):
        self.request.side_effect = [response(429, retry_after="12.5"), response(201, {"product": {"id": 123}})]
        result = shopify.create_shopify_product({"title": "Test"})
        self.assertEqual(result["product"]["id"], 123)
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(self.throttle.call_count, 2)
        self.sleep.assert_called_once_with(12.5)
        self.assertEqual(self.request.call_args_list[0].kwargs["json"], self.request.call_args_list[1].kwargs["json"])

    def test_persistent_throttle_is_bounded_and_has_clear_error(self):
        self.request.return_value = response(429, retry_after="2")
        with self.assertRaises(HTTPException) as error:
            shopify.create_shopify_product({"title": "Test"})
        self.assertEqual(error.exception.status_code, 429)
        self.assertIn("automatic retries", error.exception.detail)
        self.assertEqual(self.request.call_count, 5)
        self.assertEqual(self.sleep.call_count, 4)

    def test_uncertain_create_is_not_repeated(self):
        for outcome in [requests.Timeout("Timed out"), response(503)]:
            self.request.reset_mock()
            self.request.side_effect = [outcome]
            with self.assertRaises(HTTPException):
                shopify.create_shopify_product({"title": "Test"})
            self.assertEqual(self.request.call_count, 1)

    def test_validation_failure_is_not_retried(self):
        self.request.return_value = response(422, {"errors": "Invalid option"})
        with self.assertRaises(HTTPException) as error:
            shopify.create_shopify_product({"title": "Test"})
        self.assertIn("Invalid option", error.exception.detail)
        self.assertEqual(self.request.call_count, 1)

    def test_invalid_retry_header_uses_finite_backoff(self):
        for header in [None, "bad", "nan", "inf", "-3"]:
            self.sleep.reset_mock()
            self.request.side_effect = [response(429, retry_after=header), response(200)]
            shopify._shopify_request("GET", "https://test")
            self.sleep.assert_called_once_with(1)


class DuplicateCheckTests(unittest.TestCase):
    def test_failed_lookup_does_not_mean_product_is_missing(self):
        with patch.object(shopify.settings, "SHOPIFY_STORE", "test"), \
             patch.object(shopify, "_h", return_value={}), \
             patch.object(shopify, "_shopify_request", return_value=response(429)):
            with self.assertRaises(HTTPException):
                shopify.check_product_exists_in_shopify("Test")


if __name__ == "__main__":
    unittest.main()
