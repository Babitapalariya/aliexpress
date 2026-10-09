import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from app import aliexpress_limit as limit


class CooldownTests(unittest.TestCase):
    def setUp(self):
        limit._blocked_until = limit._next_call = 0

    def test_ban_blocks_calls_until_expiry(self):
        callback = Mock(return_value={"error_response": {"code": "AppApiCallLimit", "msg": "This ban will last 14229 seconds"}})
        with patch.object(limit.time, "monotonic", return_value=100):
            with self.assertRaises(HTTPException) as error:
                limit.guarded_call(callback)
            self.assertEqual(error.exception.headers["Retry-After"], "14229")
            with self.assertRaises(HTTPException):
                limit.guarded_call(callback)
        callback.assert_called_once()
        callback.return_value = {"result": "ok"}
        with patch.object(limit.time, "monotonic", return_value=14330):
            self.assertEqual(limit.guarded_call(callback), {"result": "ok"})

    def test_other_errors_are_not_treated_as_bans(self):
        result = {"error_response": {"code": "InvalidToken"}}
        self.assertEqual(limit.guarded_call(lambda: result), result)
        self.assertEqual(limit._blocked_until, 0)

    def test_pacing(self):
        limit._next_call = 102
        with patch.object(limit.time, "monotonic", return_value=100), patch.object(limit.time, "sleep") as sleep:
            limit.guarded_call(lambda: {})
            sleep.assert_called_once_with(2)
