import unittest

from aitrade.booster.x_news_radar import XNewsSentimentRadar


class Intraday33NewsNlpTests(unittest.TestCase):
    def setUp(self):
        self.radar = XNewsSentimentRadar()

    def test_morphology_catches_hackers_breach_drainer_exploiter(self):
        for txt in (
            "Binance lost $6.3bn to hackers in major incident",
            "Protocol suffers security breach as drainer empties pools",
            "DeFi exploiter moves funds after rugpull",
            "Bitcoin briefly drops to $17K on Kraken flash crash",
        ):
            score, trig, _ = self.radar._score_text(txt)
            self.assertLessEqual(score, -20, msg=f"Expected bearish for: {txt}, got {score} ({trig})")

    def test_macro_and_regulatory_context(self):
        # Медвежьи макро- и SEC-новости
        s_tariff, _, _ = self.radar._score_text("New 25% tariffs announced amid escalating trade war")
        self.assertLess(s_tariff, -30)
        s_cpi, _, _ = self.radar._score_text("Hot CPI report triggers hawkish Fed expectations")
        self.assertLess(s_cpi, -30)
        s_sec_sue, _, _ = self.radar._score_text("SEC charges major crypto exchange with fraud")
        self.assertLess(s_sec_sue, -40)

        # Бычьи макро- и SEC-новости не должны получать ложный штраф
        s_cut, _, _ = self.radar._score_text("Fed cuts rates by 50 bps as CPI cools below forecast")
        self.assertGreater(s_cut, 40)
        s_sec_ok, trig_ok, _ = self.radar._score_text("SEC approves spot Solana ETF with strong inflows")
        self.assertGreater(s_sec_ok, 40)
        self.assertNotIn("-sec", trig_ok)
        s_pause, trig_p, _ = self.radar._score_text("Tariff pause announced for 90 days")
        self.assertGreater(s_pause, 0)
        self.assertNotIn("-tariff", trig_p)

    def test_depeg_triggers_panic_and_negation_still_works(self):
        s_depeg, _, is_panic = self.radar._score_text("Major stablecoin depegs sharply as withdrawals suspended")
        self.assertLess(s_depeg, -50)
        self.assertTrue(is_panic)

        s_neg, _, is_panic_neg = self.radar._score_text("Stablecoin depeg rumor was false. Hackers were not involved.")
        self.assertEqual(s_neg, 0)
        self.assertFalse(is_panic_neg)


if __name__ == "__main__":
    unittest.main()
