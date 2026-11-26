import unittest

from aitrade.config import load_settings
from aitrade.execution.audit_metrics import LIVE_ACTIVE_STRATEGIES, TRACKED_STRATEGIES


class TestIntraday47DisableB3(unittest.TestCase):
    def test_b3_is_removed_from_demo_lab_and_active_tracking(self) -> None:
        settings = load_settings()
        admitted = (settings.get("demo_lab") or {}).get("admit_experimental_strategies") or []
        self.assertNotIn("B3_VWAP_MOMENTUM_IMPULSE", admitted)
        self.assertNotIn("B3_VWAP_MOMENTUM_IMPULSE", TRACKED_STRATEGIES)
        self.assertNotIn("B3_VWAP_MOMENTUM_IMPULSE", LIVE_ACTIVE_STRATEGIES)

    def test_engine_does_not_generate_b3_signals_or_diagnostics(self) -> None:
        import inspect
        from aitrade import engine

        src = inspect.getsource(engine.AITradeCombineEngine.scan_market)
        self.assertNotIn("generate_vwap_momentum_signal", src)
        self.assertNotIn('"B3_VWAP_MOMENTUM_IMPULSE"', src.split("tier_ranks")[0])


if __name__ == "__main__":
    unittest.main()
