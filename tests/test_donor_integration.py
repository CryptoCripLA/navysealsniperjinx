import unittest
from jinx_profit.donor_d1ff1cult11 import donor_pnl_records, build_labeled_dataset, donor_slot_comparison

class DonorIntegrationTests(unittest.TestCase):
    def tx(self, mint, ts):
        return {"token_address": mint, "blockTime": ts,
          "transaction":{"signatures":["sig"],"message":{"accountKeys":[mint],"instructions":[]}},
          "meta":{"fee":5,"computeUnitsConsumed":15,"innerInstructions":[
           {"instructions":[{"parsed":{"type":"initializeMint2","info":{"mint":mint}}}]}]}}
    def test_original_buy_sell_economics(self):
        rows=list(donor_pnl_records([
            {"token_address":"m","event_type":"buy","cost_usd":5},
            {"token_address":"m","event_type":"buy","cost_usd":3},
            {"token_address":"m","event_type":"sell","cost_usd":12}]))
        self.assertEqual(rows[0]["profit_usd"],4)
        self.assertEqual(rows[0]["roi_pct"],50)
    def test_no_future_and_requires_closed_trade(self):
        deployments=[self.tx("m",100),self.tx("n",200)]
        trades=[{"token_address":"m","event_type":"buy","cost_usd":5},
                {"token_address":"m","event_type":"sell","cost_usd":8},
                {"token_address":"n","event_type":"buy","cost_usd":4}]
        rows=build_labeled_dataset(deployments,trades)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["timestamp"],100)
        self.assertEqual(rows[0]["profit_usd"],3)
    def test_original_slot_sensitivity(self):
        row={"total_invested_usd":100,"total_recovered_usd":150}
        output=donor_slot_comparison([row],[True])
        self.assertEqual(output[0]["roi_pct"],50)
        self.assertAlmostEqual(output[1]["roi_pct"],(150-102)/102*100)
        self.assertEqual(output[2]["trades"],1)
    def test_selected_only(self):
        rows=[{"total_invested_usd":100,"total_recovered_usd":150},
              {"total_invested_usd":100,"total_recovered_usd":0}]
        self.assertEqual(donor_slot_comparison(rows,[True,False])[0]["pnl_usd"],50)

if __name__=="__main__": unittest.main()
