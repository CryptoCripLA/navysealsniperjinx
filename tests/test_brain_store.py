import tempfile
import unittest
from pathlib import Path
from jinx_profit.brain import ProfitBrain, Phase
from jinx_profit.brain_store import BrainStore
from jinx_profit.order_journal import OrderJournal
from jinx_profit.runner import JinxRunner, MarketOpportunity
from jinx_profit.execution_port import ExecutionReceipt

class Venue:
 def __init__(self):self.calls=0;self.fills={}
 def submit(self,intent):self.calls+=1;return "tx1"
 def get_confirmed_fill(self,tx):return self.fills.get(tx)
 def get_remaining_quantity(self,mint):return 0

class Snapshots(unittest.TestCase):
 def test_snapshot_restores_known_pending_and_reconciles(self):
  with tempfile.TemporaryDirectory() as directory:
   store=BrainStore(Path(directory)/"brain.json")
   journal=OrderJournal(Path(directory)/"journal.json")
   v=Venue();b=ProfitBrain(mode="COPY",wallet_usd=20)
   intent=b.propose_buy(source="COPY",mint="m",available_buy_usd=5,expected_net_exit_usd=8,
                         leader_verified=True,leader_expected_net_usd=1)
   store.save(b)
   journal.begin(intent);journal.submitted("tx1")
   restored=store.load()
   r=JinxRunner(restored,v,journal)
   self.assertEqual(r.submitted_tx,"tx1")
   self.assertFalse(r.reconcile())
   self.assertEqual(v.calls,0)
   v.fills["tx1"]=ExecutionReceipt("m","BUY","tx1",10,5,0)
   self.assertTrue(r.reconcile())
   self.assertEqual(restored.phase,Phase.OPEN)
   self.assertAlmostEqual(restored.wallet_usd,15)
 def test_preserves_open_inventory(self):
  with tempfile.TemporaryDirectory() as directory:
   b=ProfitBrain(mode="COPY",wallet_usd=20)
   b.propose_buy(source="COPY",mint="m",available_buy_usd=5,expected_net_exit_usd=8,
                 leader_verified=True,leader_expected_net_usd=1)
   b.apply_buy_fill(mint="m",qty=10,gross_paid_usd=5,fees_usd=0)
   store=BrainStore(Path(directory)/"brain.json");store.save(b)
   restored=store.load()
   self.assertEqual(restored.position.qty,10)
   self.assertEqual(restored.phase,Phase.OPEN)
if __name__=="__main__":unittest.main()
