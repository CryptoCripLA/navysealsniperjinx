import unittest
from jinx_profit.brain import ProfitBrain,Phase

class Model:
    def predict_proba(self, x):return [[.1,.9]]
class Size:
    def predict(self,x):return [3.9]

class BrainTests(unittest.TestCase):
    def deployment(self):
        return {"blockTime":10,"transaction":{"signatures":["s"],"message":{"accountKeys":["a"],"instructions":[]}},"meta":{"fee":5,"innerInstructions":[{"instructions":[{"parsed":{"type":"initializeMint2"}}]}]}}
    def test_auto_buy_exit_and_realized_dollars(self):
        b=ProfitBrain(mode="AUTO",wallet_usd=100, reserve_usd=5,profit_model=Model(),sizing_model=Size())
        kw=dict(source="AUTO",mint="m",deployment=self.deployment(),timestamp=12,available_buy_usd=40,expected_net_exit_usd=70,expected_total_fees_usd=2)
        self.assertEqual(b.propose_buy(**kw).action,"BUY")
        self.assertEqual(b.propose_buy(**kw).action,"WAIT")
        b.apply_buy_fill(mint="m",qty=10,gross_paid_usd=40,fees_usd=1)
        self.assertEqual(b.observe_exit(mint="m",executable_full_sell_usd=60).action,"HOLD")
        self.assertEqual(b.observe_exit(mint="m",executable_full_sell_usd=50).action,"SELL")
        b.apply_sell_fill(mint="m",qty_sold=10,gross_received_usd=50,fees_usd=1,verified_remaining_qty=0)
        self.assertEqual(b.phase,Phase.FLAT)
        self.assertAlmostEqual(b.realized_usd,8)
        self.assertAlmostEqual(b.wallet_usd,108)
        self.assertEqual(b.closed_trades,1)
    def test_copy_same_lifecycle(self):
        b=ProfitBrain(mode="COPY",wallet_usd=20)
        k=dict(source="COPY",mint="m",available_buy_usd=5,expected_net_exit_usd=12,leader_verified=True,leader_expected_net_usd=1)
        self.assertEqual(b.propose_buy(**k).action,"BUY")
        b.apply_buy_fill(mint="m",qty=10,gross_paid_usd=5,fees_usd=0)
        self.assertEqual(b.observe_exit(mint="m",executable_full_sell_usd=12,force=True).action,"SELL")
        b.apply_sell_fill(mint="m",qty_sold=10,gross_received_usd=12,fees_usd=0,verified_remaining_qty=0)
        self.assertEqual(b.realized_usd,7)
    def test_off_blocks(self):
        b=ProfitBrain(mode="OFF",wallet_usd=100)
        self.assertEqual(b.propose_buy(source="AUTO",mint="m",available_buy_usd=5,expected_net_exit_usd=10).action,"WAIT")
    def test_quote_not_profitable(self):
        b=ProfitBrain(mode="COPY",wallet_usd=100)
        self.assertEqual(b.propose_buy(source="COPY",mint="m",available_buy_usd=5,expected_net_exit_usd=4,leader_verified=True,leader_expected_net_usd=5).action,"WAIT")
    def test_partial_sell_cannot_free_position(self):
        b=ProfitBrain(mode="COPY",wallet_usd=30)
        b.propose_buy(source="COPY",mint="m",available_buy_usd=10,expected_net_exit_usd=12,leader_verified=True,leader_expected_net_usd=1)
        b.apply_buy_fill(mint="m",qty=10,gross_paid_usd=10,fees_usd=0)
        b.observe_exit(mint="m",executable_full_sell_usd=12,force=True)
        b.apply_sell_fill(mint="m",qty_sold=5,gross_received_usd=6,fees_usd=0,verified_remaining_qty=5)
        self.assertEqual(b.phase,Phase.OPEN)
        self.assertEqual(b.propose_buy(source="COPY",mint="other",available_buy_usd=3,expected_net_exit_usd=6,leader_verified=True,leader_expected_net_usd=1).action,"WAIT")

if __name__=="__main__":unittest.main()
