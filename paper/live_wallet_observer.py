#!/usr/bin/env python3
"""Read-only Solana wallet COPY observation. No signing or transaction submission.
Writes raw event evidence; does not claim fills, net profit, or 12x performance.
"""
import argparse, json, os, time, urllib.request, urllib.error
from datetime import datetime, timezone

def rpc(url, method, params):
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req=urllib.request.Request(url, data=body,headers={"Content-Type":"application/json","User-Agent":"JINX-paper-observer/1"})
    with urllib.request.urlopen(req,timeout=20) as r:
        res=json.load(r)
    if "error" in res: raise RuntimeError(str(res["error"]))
    return res.get("result")

def events(tx, wallet, sig):
    if not tx or not tx.get("meta") or tx["meta"].get("err"): return []
    meta=tx["meta"]; before={}; after={}
    for key,target in (("preTokenBalances",before),("postTokenBalances",after)):
        for b in meta.get(key) or []:
            if b.get("owner")==wallet:
                mint=b.get("mint")
                val=(b.get("uiTokenAmount") or {}).get("amount")
                if mint and val is not None:
                    target[mint]=target.get(mint,0)+int(val)
    out=[]
    for mint in set(before)|set(after):
        delta=after.get(mint,0)-before.get(mint,0)
        if delta:
            out.append({"signature":sig,"mint":mint,"leader":wallet,"action":"BUY" if delta>0 else "SELL",
              "token_delta_raw":str(delta),"slot":tx.get("slot"),"block_time":tx.get("blockTime"),
              "source":"onchain_wallet_token_balance_delta","paper_order_sent":False})
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--wallet",default="5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr")
    ap.add_argument("--rpc",default=os.environ.get("SOLANA_RPC_URL","https://api.mainnet-beta.solana.com"))
    ap.add_argument("--seconds",type=int,default=180)
    ap.add_argument("--poll",type=int,default=15)
    ap.add_argument("--out",default="paper-observations.jsonl")
    args=ap.parse_args()
    if args.seconds<1 or args.seconds>600: ap.error("--seconds must be 1..600")
    seen=set()
    started=time.monotonic(); count=0; faults=0
    while time.monotonic()-started<args.seconds:
        try:
            sigs=rpc(args.rpc,"getSignaturesForAddress",[args.wallet,{"limit":20,"commitment":"confirmed"}]) or []
            for entry in reversed(sigs):
                sig=entry["signature"]
                if sig in seen: continue
                seen.add(sig)
                tx=rpc(args.rpc,"getTransaction",[sig,{"encoding":"jsonParsed","maxSupportedTransactionVersion":0,"commitment":"confirmed"}])
                for event in events(tx,args.wallet,sig):
                    with open(args.out,"a",encoding="utf-8") as f: f.write(json.dumps(event)+"\n")
                    count+=1
                    print(json.dumps(event),flush=True)
        except (RuntimeError,urllib.error.URLError,TimeoutError,ValueError) as e:
            faults+=1
            print("RPC_OBSERVATION_ERROR",type(e).__name__,str(e)[:180],flush=True)
        time.sleep(min(args.poll,max(0,args.seconds-(time.monotonic()-started))))
    print(json.dumps({"observations":count,"rpc_errors":faults,"note":"READ ONLY; no paper fills created, not a profit test"}),flush=True)

if __name__=="__main__": main()
