import unittest
from jinx_profit.donor_kaizen256 import deployer_relationship_features

class KaizenIntegration(unittest.TestCase):
    def test_prior_only_and_deployment_gap(self):
        launches=[{"timestamp":10},{"timestamp":20},{"timestamp":30},{"timestamp":40},{"timestamp":50}]
        buys=[{"timestamp":20},{"timestamp":40},{"timestamp":60}]
        a=deployer_relationship_features(50,launches,buys)
        self.assertEqual(a["prior_target_buy_count"],2)
        self.assertEqual(a["deployments_since_prior_target_buy"],0)
        self.assertEqual(a["seconds_since_prior_target_buy"],10)
        b=deployer_relationship_features(60,launches,buys)
        self.assertEqual(b["deployments_since_prior_target_buy"],1)
        self.assertEqual(b["prior_target_buy_count"],2)
    def test_empty_history(self):
        result=deployer_relationship_features(100,[],[])
        self.assertEqual(result["target_signer_known"],0)
        self.assertEqual(result["prior_target_buy_count_1h"],0)
        self.assertEqual(result["prior_target_buy_fraction"],0)
    def test_time_windows(self):
        r=deployer_relationship_features(100000,[{"timestamp":1},{"timestamp":99900}],
            [{"timestamp":1},{"timestamp":99900}])
        self.assertEqual(r["prior_target_buy_count_1h"],1)
        self.assertEqual(r["prior_target_buy_count_1d"],1)
        self.assertEqual(r["prior_target_buy_count_7d"],2)
if __name__=="__main__":unittest.main()
