"""Durable pending-order journal; fail closed on ambiguous network submissions.

Never submit a second transaction after a crash just because a tx signature
was not persisted. The venue must first resolve a previous submitting intent.
"""
import json
import os
from pathlib import Path

class OrderJournal:
    def __init__(self, path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)

    def read(self):
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, record):
        tmp=self.path.with_suffix(self.path.suffix+".tmp")
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(record,f,sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,self.path)
        try:
            fd=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except OSError:
            pass

    def begin(self, intent):
        if self.read() is not None:
            raise RuntimeError("unresolved previous order; reconcile before submission")
        self.write({"state":"SUBMITTING","action":intent.action,"mint":intent.mint,
                    "usd":intent.usd})

    def submitted(self, tx_id):
        item=self.read()
        if item is None or item["state"]!="SUBMITTING":
            raise RuntimeError("no submitting intent")
        item["state"]="PENDING"
        item["tx_id"]=str(tx_id)
        self.write(item)

    def clear(self):
        if self.path.exists():
            self.path.unlink()
