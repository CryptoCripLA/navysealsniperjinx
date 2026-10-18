"""Read-only Solana RPC observations for startup reconciliation.

No signing, transaction submission, or inferred USD accounting.
"""
import json
from urllib.request import Request, urlopen

class SolanaRpcReadOnly:
    def __init__(self, url, *, timeout=5):
        if not url.startswith(("https://","http://127.0.0.1:","http://localhost:")):
            raise ValueError("expected HTTPS RPC endpoint")
        self.url=url
        self.timeout=timeout

    def call(self, method, params):
        body=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
        request=Request(self.url,data=body,headers={"Content-Type":"application/json"})
        with urlopen(request,timeout=self.timeout) as response:
            result=json.load(response)
        if result.get("error"):
            raise RuntimeError(f"RPC {method}: {result['error']}")
        if "result" not in result:
            raise RuntimeError(f"RPC {method}: missing result")
        return result["result"]

    def signature_status(self, signature):
        values=self.call("getSignatureStatuses",[[signature],{"searchTransactionHistory":True}])
        status=values["value"][0]
        if status is None:return "UNKNOWN"
        if status["err"] is not None:return "FAILED"
        if status.get("confirmationStatus")=="finalized":return "FINALIZED"
        if status.get("confirmationStatus")=="confirmed":return "CONFIRMED"
        return "PENDING"

    def token_quantity(self, owner, mint):
        result=self.call("getTokenAccountsByOwner",[owner,{"mint":mint},
                          {"encoding":"jsonParsed","commitment":"confirmed"}])
        total=0
        scale=None
        for account in result["value"]:
            amount=account["account"]["data"]["parsed"]["info"]["tokenAmount"]
            decimals=int(amount["decimals"])
            if scale is not None and scale!=decimals:
                raise ValueError("conflicting token decimals")
            scale=decimals
            total+=int(amount["amount"])
        return total/(10**scale) if scale is not None else 0.0

def validate_restored_inventory(brain, rpc, owner, *, tolerance=1e-9):
    """Reject unavailable/mismatched account state; never overwrite local P&L."""
    if brain.position is None:
        return True
    actual=rpc.token_quantity(owner,brain.position.mint)
    if abs(actual-brain.position.qty)>tolerance:
        raise RuntimeError("restored position disagrees with confirmed token inventory")
    return True
