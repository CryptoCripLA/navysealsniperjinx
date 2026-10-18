import unittest
from jinx_profit.brain import ProfitBrain, Phase
from jinx_profit.runner import JinxRunner, MarketOpportunity
from jinx_profit.execution_port import ExecutionReceipt

class Venue:
 def __init__(self):
  self.sent=[];self.fills={};self.holdings={}
 def submit(self,intent):
  self.sent.append(intent)
  return "tx"+str(len(self.sent))
 def get_confirmed_fill(self,tx):
  return self.fills.get(tx)
 def get_remaining_quantity(self,mint):
  return self.holdings.get(mint,0)

class RunnerTests(unittest.TestCase):
 def test_one_buy_one_sell_in_usd(self):
  b=ProfitBrain(mode="COPY",wallet_usd=20)
  v=Venue(); r=JinxRunner(b,v)
  event=MarketOpportunity("COPY","mint",5,12,0,100,leader_verified=True,leader_expected_net_usd=4)
  self.assertEqual(r.on_opportunity(event).action,"BUY")
  self.assertIsNone(r.on_opportunity(event))
  self.assertEqual(len(v.sent),1)
  self.assertFalse(r.reconcile())
  self.assertEqual(b.phase,Phase.BUY_PENDING)
  v.fills["tx1"]=ExecutionReceipt("mint","BUY","tx1",10,5,0)
  self.assertTrue(r.reconcile())
  self.assertEqual(r.on_position_quote(mint="mint",full_exit_usd=12,fees_usd=0).action,"HOLD")
  self.assertEqual(r.on_position_quote(mint="mint",full_exit_usd=10,fees_usd=0).action,"SELL")
  self.assertEqual(len(v.sent),2)
  v.holdings["mint"]=0
  v.fills["tx2"]=ExecutionReceipt("mint","SELL","tx2",10,10,0,0)
  self.assertTrue(r.reconcile())
  self.assertEqual(b.realized_usd,5)
  self.assertEqual(b.closed_trades,1)
  self.assertEqual(b.phase,Phase.FLAT)
 def test_off_blocks_entry(self):
  b=ProfitBrain(mode="OFF",wallet_usd=20);v=Venue();r=JinxRunner(b,v)
  self.assertEqual(r.on_opportunity(MarketOpportunity("AUTO","mint",5,12,0,100)).action,"WAIT")
  self.assertEqual(v.sent,[])
if __name__=="__main__":unittest.main()
