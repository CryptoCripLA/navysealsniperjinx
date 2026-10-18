import tempfile
import unittest
from pathlib import Path
from jinx_profit.brain import ProfitBrain
from jinx_profit.runner import JinxRunner, MarketOpportunity
from jinx_profit.order_journal import OrderJournal
from jinx_profit.execution_port import ExecutionReceipt

class Venue:
 def __init__(self):self.receipts={};self.calls=0
 def submit(self,intent):self.calls+=1;return "tx"+str(self.calls)
 def get_confirmed_fill(self,tx):return self.receipts.get(tx)
 def get_remaining_quantity(self,mint):return 0

class DurableRunnerTests(unittest.TestCase):
 def test_pending_survives_restart_without_resubmission(self):
  with tempfile.TemporaryDirectory() as d:
   j=OrderJournal(Path(d)/"pending.json")
   b=ProfitBrain(mode="COPY",wallet_usd=20);v=Venue()
   r=JinxRunner(b,v,j)
   r.on_opportunity(MarketOpportunity("COPY","m",5,12,0,100,leader_verified=True,leader_expected_net_usd=1))
   self.assertEqual(j.read()["tx_id"],"tx1")
   self.assertEqual(v.calls,1)
   with self.assertRaises(RuntimeError):JinxRunner(ProfitBrain(mode="COPY",wallet_usd=20),v,OrderJournal(Path(d)/"pending.json"))
   self.assertEqual(v.calls,1)
   v.receipts["tx1"]=ExecutionReceipt("m","BUY","tx1",10,5,0)
   self.assertTrue(r.reconcile())
   self.assertIsNone(j.read())
 def test_submission_failure_leaves_uncertain_journal(self):
  with tempfile.TemporaryDirectory() as d:
   class BadVenue(Venue):
    def submit(self,intent):raise TimeoutError("unknown broadcast result")
   j=OrderJournal(Path(d)/"order.json")
   r=JinxRunner(ProfitBrain(mode="COPY",wallet_usd=20),BadVenue(),j)
   with self.assertRaises(TimeoutError):
    r.on_opportunity(MarketOpportunity("COPY","m",5,12,0,100,leader_verified=True,leader_expected_net_usd=1))
   self.assertEqual(j.read()["state"],"SUBMITTING")
if __name__=="__main__":unittest.main()
