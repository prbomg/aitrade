import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from aitrade.config import BASE_DIR
from aitrade.dashboard.live_server import KairosSessionAuth, create_wsgi_app


def _call_wsgi(app, method="GET", path="/", query="", body=None, headers=None):
    raw = b""
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json" if body is not None else "",
        "HTTP_HOST": "127.0.0.1:8765",
        "REMOTE_ADDR": "127.0.0.1",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(raw),
    }
    if headers:
        for k, v in headers.items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
    captured = {}

    def start_response(status, resp_headers):
        captured["status"] = status
        captured["headers"] = dict(resp_headers)
        captured["raw_headers"] = list(resp_headers)

    chunks = app(environ, start_response)
    return captured["status"], captured["headers"], b"".join(chunks)


class Intraday35KairosBrandingAuthTests(unittest.TestCase):
    def setUp(self):
        self.state = MagicMock()
        self.state.health.return_value = {"ok": True, "version": "2026.11.22-intraday.35"}
        self.state.cached_state.return_value = {"ok": True, "timestamp": "2026-11-22 12:00:00"}
        self.state.release_manifest = {"version": "2026.11.22-intraday.35"}

    def test_kairos_logo_and_home_link_present_in_html_and_js(self):
        html = (BASE_DIR / "dashboard_report.html").read_text(encoding="utf-8")
        js = (BASE_DIR / "dashboard_app.js").read_text(encoding="utf-8")
        login_html = (BASE_DIR / "login.html").read_text(encoding="utf-8")
        svg = (BASE_DIR / "kairos_logo.svg").read_text(encoding="utf-8")

        self.assertIn("<title>KAIROS", html)
        self.assertIn('href="/favicon.svg"', html)
        self.assertIn('id="brand-home-link"', html)
        self.assertIn('href="/"', html)
        self.assertIn("KAIROS", html)
        self.assertIn("brand-home-link", js)
        self.assertIn("btn-logout", js)
        self.assertIn("/api/auth/login", login_html)
        self.assertIn("KAIROS", login_html)
        self.assertIn("kairosBeamGrad", svg)

    def test_static_routes_for_logo_and_login_work_without_auth(self):
        with patch.dict(os.environ, {"KAIROS_AUTH_USER": "", "KAIROS_AUTH_PASSWORD": ""}, clear=False):
            app = create_wsgi_app(self.state)
            st_svg, hdr_svg, body_svg = _call_wsgi(app, "GET", "/favicon.svg")
            self.assertTrue(st_svg.startswith("200"))
            self.assertIn("image/svg+xml", hdr_svg["Content-Type"])
            self.assertIn(b"<svg", body_svg)

            st_login, hdr_login, body_login = _call_wsgi(app, "GET", "/login")
            self.assertTrue(st_login.startswith("200"))
            self.assertIn(b"KAIROS", body_login)

            st_root, _, _ = _call_wsgi(app, "GET", "/")
            self.assertTrue(st_root.startswith("200"))

    def test_session_auth_gate_login_and_logout_flow(self):
        env_patch = {
            "KAIROS_AUTH_USER": "admin",
            "KAIROS_AUTH_PASSWORD": "secret-pass-42",
            "KAIROS_AUTH_SECRET": "unit-test-secret-key",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            app = create_wsgi_app(self.state)

            # Healthz and /login stay accessible
            st_h, _, _ = _call_wsgi(app, "GET", "/healthz")
            self.assertTrue(st_h.startswith("200"))
            st_l, _, _ = _call_wsgi(app, "GET", "/login")
            self.assertTrue(st_l.startswith("200"))

            # Unauthenticated GET / redirects to /login
            st_root, hdr_root, _ = _call_wsgi(app, "GET", "/")
            self.assertTrue(st_root.startswith("302"))
            self.assertTrue(hdr_root["Location"].startswith("/login"))

            # Unauthenticated API returns 401
            st_api, _, _ = _call_wsgi(app, "GET", "/api/state")
            self.assertTrue(st_api.startswith("401"))

            # Wrong password -> 401
            st_bad, _, _ = _call_wsgi(
                app,
                "POST",
                "/api/auth/login",
                body={"username": "admin", "password": "wrong"},
                headers={"X-Requested-With": "AItrade"},
            )
            self.assertTrue(st_bad.startswith("401"))

            # Correct password -> 200 + Set-Cookie
            st_ok, hdr_ok, body_ok = _call_wsgi(
                app,
                "POST",
                "/api/auth/login",
                body={"username": "admin", "password": "secret-pass-42", "remember": True},
                headers={"X-Requested-With": "AItrade"},
            )
            self.assertTrue(st_ok.startswith("200"))
            self.assertTrue(json.loads(body_ok)["ok"])
            cookie_val = hdr_ok["Set-Cookie"].split(";", 1)[0]
            self.assertTrue(cookie_val.startswith(KairosSessionAuth.COOKIE_NAME + "="))

            # Authenticated GET / and /api/state succeed
            st_root2, _, _ = _call_wsgi(app, "GET", "/", headers={"Cookie": cookie_val})
            self.assertTrue(st_root2.startswith("200"))
            st_api2, _, _ = _call_wsgi(app, "GET", "/api/state", headers={"Cookie": cookie_val})
            self.assertTrue(st_api2.startswith("200"))

            # Logout clears cookie
            st_out, hdr_out, _ = _call_wsgi(
                app,
                "POST",
                "/api/auth/logout",
                body={},
                headers={"X-Requested-With": "AItrade", "Cookie": cookie_val},
            )
            self.assertTrue(st_out.startswith("200"))
            self.assertIn("Max-Age=0", hdr_out["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
