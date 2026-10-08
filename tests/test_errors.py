import unittest
from utils import is_transient_error, get_http_status_code

class DummyHttpError(Exception):
    def __init__(self, status, reason_str=""):
        self.resp = type('Resp', (), {'status': status})()
        self.content = reason_str.encode('utf-8')
        super().__init__(f"HTTP {status}: {reason_str}")

class DummyAPIError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"API Error {code}")

class TestErrorClassification(unittest.TestCase):
    def test_transient_status_codes(self):
        for code in (408, 429, 500, 502, 503, 504):
            err = DummyHttpError(code)
            self.assertTrue(is_transient_error(err), f"Code {code} should be transient")

    def test_permanent_status_codes(self):
        for code in (400, 401, 404):
            err = DummyHttpError(code)
            self.assertFalse(is_transient_error(err), f"Code {code} should be permanent")

    def test_google_reason_extraction(self):
        transient_err = DummyHttpError(403, '{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}')
        self.assertTrue(is_transient_error(transient_err))

        user_transient_err = DummyHttpError(403, '{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}')
        self.assertTrue(is_transient_error(user_transient_err))

        perm_err = DummyHttpError(403, '{"error": {"errors": [{"reason": "storageQuotaExceeded"}]}}')
        self.assertFalse(is_transient_error(perm_err))

        perm_perm_err = DummyHttpError(403, '{"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}')
        self.assertFalse(is_transient_error(perm_perm_err))

    def test_connection_and_timeout_errors(self):
        self.assertTrue(is_transient_error(TimeoutError("Connection timed out")))
        self.assertTrue(is_transient_error(ConnectionError("Connection reset")))

if __name__ == "__main__":
    unittest.main()
