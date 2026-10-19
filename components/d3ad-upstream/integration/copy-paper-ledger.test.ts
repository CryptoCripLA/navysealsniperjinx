import { describe, expect, it } from 'vitest';
import { mkdtempSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { parseTransactionData } from '../../mortdeus-upstream/2_copy trading bot(node) using gRPC/parsingtransaction.js';
import { CopyPaperLedger } from './copy-paper-ledger';

function leaderEvent(buy: boolean): Buffer {
  const data = Buffer.alloc(233);
  Buffer.alloc(32, 15).copy(data, 16);
  data.writeBigUInt64LE(100_000_000n, 48);
  data.writeBigUInt64LE(5_000_000_000n, 56);
  data[64] = buy ? 1 : 0;
  Buffer.alloc(32, 31).copy(data, 65);
  data.writeBigUInt64LE(70_000_000_000n, 105);
  data.writeBigUInt64LE(1_000_000_000_000n, 113);
  return data;
}
describe('Persistent original-donor COPY-to-exit paper ledger', () => {
 it('enforces one slot, dedupes, persists across restart, closes at full quote and zero residual', () => {
   const file = path.join(mkdtempSync(path.join(os.tmpdir(), 'jinx-copy-')), 'ledger.json');
   const buy = parseTransactionData(leaderEvent(true));
   const sell = parseTransactionData(leaderEvent(false));
   const a = new CopyPaperLedger(file);
   expect(a.onBuy({mint:buy.mint,leader:buy.user,signature:'buy1',tokens:BigInt(buy.tokenchange),costLamports:BigInt(buy.solchange)})).toBe(true);
   expect(a.onBuy({mint:buy.mint,leader:buy.user,signature:'buy2',tokens:2n,costLamports:1n})).toBe(false);
   const b = new CopyPaperLedger(file);
   expect(b.state.position?.entrySignature).toBe('buy1');
   const end = b.onSell({mint:sell.mint,leader:sell.user,signature:'sell1',
     virtualSol:BigInt(sell.context.virtualSolReserves),virtualTokens:BigInt(sell.context.virtualTokenReserves)});
   expect(end).not.toBeNull();
   expect(BigInt(end!.exitLamports)).toBeGreaterThan(0n);
   expect(end!.residualTokens).toBe('0');
   const c = new CopyPaperLedger(file);
   expect(c.state.position).toBeNull();
   expect(c.state.closed).toHaveLength(1);
   expect(c.onSell({mint:sell.mint,leader:sell.user,signature:'sell1',virtualSol:1n,virtualTokens:1n})).toBeNull();
   expect(JSON.parse(readFileSync(file,'utf8')).closed).toHaveLength(1);
   console.log('PAPER_PERSISTENT_CLOSED', JSON.stringify(end));
 });
});
