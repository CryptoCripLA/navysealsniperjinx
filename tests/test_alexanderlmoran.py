import unittest
from jinx_profit.donor_alexanderlmoran import (
    original_exit_module, compare_historical_exits, trailing_exit_signal
)

class MoranDonorTests(unittest.TestCase):
    def test_loads_original_source(self):
        donor=original_exit_module()
        self.assertTrue(callable(donor.strategy_fixed_trail_6))
        self.assertEqual(len(donor.STRATEGIES),6)

    def test_original_exit_formulas_used(self):
        m=original_exit_module()
        envelope=m.TradeEnvelope(mint="m",entry_px=100,exit_px=110,
          high_px=150,low_px=90,mfe_pct=50,mae_pct=-10,roi_pct=10,
          hold_s=30,fees_usd=1,size_usd=100,close_reason="closed",pnl_usd=9)
        results=compare_historical_exits(envelope)
        self.assertEqual(results["trail_6"],m.strategy_fixed_trail_6(envelope))
        self.assertEqual(results["partial_trail"],m.strategy_partial_tp_trail(envelope))
        self.assertEqual(len(results),6)

    def test_no_hindsight_in_live_peak_signal(self):
        self.assertFalse(trailing_exit_signal([100,140,135,136]))
        self.assertTrue(trailing_exit_signal([100,140,135,130]))
        self.assertFalse(trailing_exit_signal([100,99]))

    def test_malformed_price_rejected(self):
        with self.assertRaises(ValueError):
            trailing_exit_signal([100,0])
if __name__=="__main__":unittest.main()
