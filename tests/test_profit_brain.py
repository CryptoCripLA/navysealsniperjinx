import unittest
from jinx_profit.features import extract_deployment_features, FEATURE_NAMES
from jinx_profit.model import chronological_split, train_profit_model, score_opportunity

class ProfitBrainTests(unittest.TestCase):
    def fixture(self, ts=100):
        return {"blockTime":ts,"transaction":{"signatures":["sig"],"message":{"accountKeys":[{"pubkey":"payer"},"mint"],"instructions":[{"parsed":{"type":"transfer","info":{"lamports":100}}},{"parsed":{"type":"transfer","info":{"lamports":450}}}]}},"meta":{"computeUnitsConsumed":12,"fee":5,"innerInstructions":[{"instructions":[{"parsed":{"type":"initializeMint2","info":{"mint":"mint"}}},{"parsed":{"type":"noop"}}]}],"logMessages":["a"],"preTokenBalances":[]}}
    def test_exact_deployment_features(self):
        f=extract_deployment_features(self.fixture())
        self.assertEqual(tuple(f.keys()),FEATURE_NAMES)
        self.assertEqual(f["dev_buy_lamports"],450)
        self.assertEqual(f["n_inner_instructions"],2)
        self.assertEqual(f["n_signers"],1)
    def test_reject_non_deployment_and_failed_tx(self):
        x=self.fixture();x["meta"]["err"]={"InstructionError":[0,"Custom"]}
        with self.assertRaises(ValueError):extract_deployment_features(x)
        x=self.fixture();x["meta"]["innerInstructions"]=[]
        with self.assertRaises(ValueError):extract_deployment_features(x)
    def test_nonnegative_features(self):
        x=self.fixture();x["meta"]["fee"]=-1
        with self.assertRaises(ValueError):extract_deployment_features(x)
    def test_actual_chronological_split(self):
        rows=[{"timestamp":8,"profit_usd":2},{"timestamp":2,"profit_usd":-1},{"timestamp":5,"profit_usd":4},{"timestamp":1,"profit_usd":8},{"timestamp":3,"profit_usd":1}]
        tr,te=chronological_split(rows,test_fraction=.4)
        self.assertEqual([r["timestamp"] for r in tr],[1,2,3])
        self.assertEqual([r["timestamp"] for r in te],[5,8])
    def test_no_training_with_single_class(self):
        rows=[dict(self.fixture(),profit_usd=1,timestamp=n) for n in range(8)]
        with self.assertRaises(ValueError):train_profit_model(rows)
    def test_no_model_means_no_automatic_buy(self):
        with self.assertRaises(ValueError):score_opportunity(None,extract_deployment_features(self.fixture()))
if __name__=="__main__":unittest.main()
