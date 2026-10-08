import json
import unittest

from dashboard_app import ALLOWED_JOB_KEYS, DashboardApplication, build_payload, sanitize_job


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            {
                "source_key": "aaaaaaaa11111111",
                "name": "Clase Primary.mp3",
                "source": "drive",
                "status": "completed",
                "completed_chunks": 4,
                "total_chunks": 4,
                "updated_at": "2026-09-07T00:00:00Z",
                "transcript": "SECRET TRANSCRIPT BODY",
                "refresh_token": "must-not-leak",
            },
            {
                "source_key": "bbbbbbbb22222222",
                "name": "Clase Secondary.mp3",
                "source": "drive",
                "status": "in_progress",
                "owner_id": "secondary",
                "completed_chunks": 2,
                "total_chunks": 5,
                "updated_at": "2026-09-07T00:01:00Z",
            },
        ]

    def test_legacy_jobs_default_to_primary(self):
        payload = build_payload(self.jobs, "primary")
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["jobs"][0]["name"], "Clase Primary.mp3")
        self.assertEqual(payload["jobs"][0]["owner_id"], "primary")

    def test_secondary_filter_is_explicit(self):
        payload = build_payload(self.jobs, "secondary")
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["jobs"][0]["name"], "Clase Secondary.mp3")
        self.assertEqual(payload["jobs"][0]["progress_pct"], 40)

    def test_api_shape_is_allowlisted(self):
        safe = sanitize_job(self.jobs[0])
        self.assertEqual(set(safe), ALLOWED_JOB_KEYS)
        serialized = json.dumps(safe)
        self.assertNotIn("SECRET TRANSCRIPT BODY", serialized)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("refresh_token", serialized)

    def test_unknown_user_is_rejected(self):
        with self.assertRaises(ValueError):
            build_payload(self.jobs, "someone-else")


class DashboardWsgiTests(unittest.TestCase):
    def call(self, app, path="/", query="", email=None):
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        environ = {"PATH_INFO": path, "QUERY_STRING": query}
        if email:
            environ["HTTP_X_GOOG_AUTHENTICATED_USER_EMAIL"] = f"accounts.google.com:{email}"
        body = b"".join(app(environ, start_response))
        return captured["status"], captured["headers"], body

    def test_health_is_available_without_iap_header(self):
        app = DashboardApplication(job_loader=lambda: [], require_iap=True)
        status, _, body = self.call(app, "/healthz")
        self.assertEqual(status, "200 OK")
        self.assertTrue(json.loads(body)["ok"])

    def test_iap_header_is_required_in_production_mode(self):
        app = DashboardApplication(job_loader=lambda: [], require_iap=True)
        status, _, _ = self.call(app, "/api/jobs", "user=primary")
        self.assertEqual(status, "401 Unauthorized")

    def test_app_allowlist_is_defense_in_depth(self):
        app = DashboardApplication(
            job_loader=lambda: [],
            require_iap=True,
            allowed_emails={"primary@example.com", "secondary@example.com"},
        )
        status, _, _ = self.call(app, "/api/jobs", "user=primary", "other@example.com")
        self.assertEqual(status, "403 Forbidden")
        status, _, body = self.call(app, "/api/jobs", "user=secondary", "secondary@example.com")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["viewer"], "secondary@example.com")

    def test_invalid_dropdown_value_returns_400(self):
        app = DashboardApplication(job_loader=lambda: [], require_iap=False)
        status, _, _ = self.call(app, "/api/jobs", "user=admin")
        self.assertEqual(status, "400 Bad Request")

    def test_html_contains_exact_two_user_options(self):
        app = DashboardApplication(job_loader=lambda: [], require_iap=False)
        status, _, body = self.call(app, "/")
        text = body.decode("utf-8")
        self.assertEqual(status, "200 OK")
        self.assertIn('<option value="primary">Primary</option>', text)
        self.assertIn('<option value="secondary">Secondary</option>', text)
        self.assertNotIn('<option value="admin">', text)


if __name__ == "__main__":
    unittest.main()
