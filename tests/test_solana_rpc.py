import unittest
from jinx_profit.solana_rpc import SolanaRpcReadOnly, validate_restored_inventory
from jinx_profit.brain import ProfitBrain

class FakeRpc(SolanaRpcReadOnly):
 def __init__(self,data):
  self.data=data
 def call(self,method,params):
  return self.data[method]
class SolanaReadTests(unittest.TestCase):
 def test_rpc_signature_confirmation(self):
  r=FakeRpc({"getSignatureStatuses":{"value":[{"err":None,"confirmationStatus":"finalized"}]}})
  self.assertEqual(r.signature_status("sig"),"FINALIZED")
  r.data["getSignatureStatuses"]={"value":[{"err":{"x":1},"confirmationStatus":"finalized"}]}
  self.assertEqual(r.signature_status("sig"),"FAILED")
 def test_token_inventory_exact_units(self):
  r=FakeRpc({"getTokenAccountsByOwner":{"value":[
    {"account":{"data":{"parsed":{"info":{"tokenAmount":{"amount":"1250000","decimals":6}}}}}},
    {"account":{"data":{"parsed":{"info":{"tokenAmount":{"amount":"750000","decimals":6}}}}}}]}})
  self.assertEqual(r.token_quantity("owner","mint"),2)
 def test_inventory_disagreement_blocks(self):
  b=ProfitBrain(mode="COPY",wallet_usd=20)
  b.propose_buy(source="COPY",mint="m",available_buy_usd=5,expected_net_exit_usd=8,leader_verified=True,leader_expected_net_usd=1)
  b.apply_buy_fill(mint="m",qty=10,gross_paid_usd=5,fees_usd=0)
  class Inventory:
   def token_quantity(self,owner,mint):return 9
  with self.assertRaises(RuntimeError):validate_restored_inventory(b,Inventory(),"owner")
if __name__=="__main__":unittest.main()
