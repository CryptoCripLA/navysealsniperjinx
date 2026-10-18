import unittest
from jinx_profit.donor_shared_adapters import (
 wallet_performance,rank_copy_wallets,mirror_sell_fraction,
 copier_expected_net_usd,executable_net_profit_usd,normalize_market_event
)
class RemainingDonorsTest(unittest.TestCase):
 def test_wallet_realized_ranking(self):
  a=wallet_performance("a",[{"net_profit_usd":3},{"net_profit_usd":-1}],100)
  b=wallet_performance("b",[{"net_profit_usd":-5}],101)
  self.assertEqual(a.realized_profit_usd,2)
  self.assertEqual(rank_copy_wallets([a,b],min_closed=2),[a])
 def test_copy_fraction(self):
  self.assertEqual(mirror_sell_fraction(50,100,75),12.5)
  with self.assertRaises(ValueError):mirror_sell_fraction(10,100,110)
 def test_copier_real_economics(self):
  self.assertEqual(copier_expected_net_usd(expected_exit_usd=15,buy_usd=10,entry_fees_usd=1,exit_fees_usd=1,leader_price_impact_usd=2),1)
  self.assertEqual(executable_net_profit_usd(buy_total_usd=10,full_exit_quote_usd=12,expected_sell_fees_usd=1),1)
 def test_past_only_market_event(self):
  self.assertEqual(normalize_market_event({"mint":"m","full_exit_quote_usd":12},event_timestamp=9,current_timestamp=10)["mint"],"m")
  with self.assertRaises(ValueError):normalize_market_event({"mint":"m","full_exit_quote_usd":12},event_timestamp=11,current_timestamp=10)
if __name__=="__main__":unittest.main()
