import unittest
from jinx_profit.brain import ProfitBrain, Phase
from jinx_profit.execution_port import SingleOrderPort, ExecutionReceipt

class FakeVenue:
    def __init__(self):
        self.receipts={}
        self.inventory={}
        self.orders=[]
    def submit(self,intent):
        self.orders.append(intent)
        return "tx-"+str(len(self.orders))
    def get_confirmed_fill(self,tx_id):
        return self.receipts.get(tx_id)
    def get_remaining_quantity(self,mint):
        return self.inventory.get(mint,0)

class SinglePortTests(unittest.TestCase):
    def test_real_receipt_contract_not_synthetic_success(self):
        b=ProfitBrain(mode="COPY",wallet_usd=20)
        v=FakeVenue()
        port=SingleOrderPort(b,v)
        b.propose_buy(source="COPY",mint="m",available_buy_usd=5,expected_net_exit_usd=10,leader_verified=True,leader_expected_net_usd=1)
        tx=port.submit_pending()
        self.assertFalse(port.reconcile(tx))
        self.assertEqual(b.phase,Phase.BUY_PENDING)
        v.receipts[tx]=ExecutionReceipt("m","BUY",tx,10,5,0)
        self.assertTrue(port.reconcile(tx))
        self.assertEqual(b.phase,Phase.OPEN)
        b.observe_exit(mint="m",executable_full_sell_usd=10,force=True)
        tx2=port.submit_pending()
        v.inventory["m"]=0
        v.receipts[tx2]=ExecutionReceipt("m","SELL",tx2,10,10,0,0)
        self.assertTrue(port.reconcile(tx2))
        self.assertEqual(b.phase,Phase.FLAT)
        self.assertEqual(b.realized_usd,5)
    def test_wrong_receipt_never_books_money(self):
        b=ProfitBrain(mode="COPY",wallet_usd=20)
        v=FakeVenue()
        port=SingleOrderPort(b,v)
        b.propose_buy(source="COPY",mint="m",available_buy_usd=5,expected_net_exit_usd=10,leader_verified=True,leader_expected_net_usd=1)
        v.receipts["a"]=ExecutionReceipt("other","BUY","a",10,5,0)
        with self.assertRaises(ValueError):
            port.reconcile("a")
        self.assertEqual(b.wallet_usd,20)
if __name__=="__main__":unittest.main()
