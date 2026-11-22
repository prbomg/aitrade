import unittest

from aitrade.config import BASE_DIR


class Intraday32UiUxAuditTests(unittest.TestCase):
    def setUp(self):
        self.html = (BASE_DIR / "dashboard_report.html").read_text(encoding="utf-8")
        self.js = (BASE_DIR / "dashboard_app.js").read_text(encoding="utf-8")

    def test_drawers_live_outside_main_dashboard_and_have_close_buttons(self):
        drawers_idx = self.html.index('class="drawers-host"')
        main_idx = self.html.index('id="main-dashboard-view"')
        self.assertLess(drawers_idx, main_idx)
        for close_id in ("btn-close-env", "btn-close-drawer", "btn-close-logs"):
            self.assertIn(f'id="{close_id}"', self.html)
            self.assertIn(close_id, self.js)

    def test_grid_min_width_zero_and_disabled_button_styles(self):
        self.assertIn("minmax(0, 1.35fr) minmax(0, 1fr)", self.html)
        self.assertIn("button:disabled", self.html)
        self.assertIn("font-variant-numeric:tabular-nums", self.html)
        self.assertNotIn("Поправка к Оценка сигнала", self.html)
        self.assertNotIn("безконфликтный", self.html)
        self.assertNotIn("/v5/position/closed-pnl", self.html)

    def test_closed_pnl_side_inversion_and_trigger_price_and_rejected_disable(self):
        self.assertIn('c.side === "Sell" ? "LONG" : (c.side === "Buy" ? "SHORT"', self.js)
        self.assertIn("o.triggerPrice", self.js)
        self.assertIn('=== "REJECTED"', self.js)
        self.assertNotIn("data-action=pyramid", self.js)
        self.assertIn("function fmtPrice(", self.js)

    def test_strategy_detail_navigation_and_refresh_lock(self):
        self.assertIn('id="sd-strategy-select"', self.html)
        self.assertIn('addEventListener("popstate"', self.js)
        self.assertIn("window._mainScrollY", self.js)
        self.assertIn("Date.now() < window._uiLockUntil", self.js)
        self.assertIn("window._toastTimer", self.js)


if __name__ == "__main__":
    unittest.main()
