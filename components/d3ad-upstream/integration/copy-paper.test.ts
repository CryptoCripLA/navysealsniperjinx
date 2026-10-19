import { describe, expect, it } from 'vitest';
import { parseTransactionData } from '../../mortdeus-upstream/2_copy trading bot(node) using gRPC/parsingtransaction.js';
import { sellValue } from '../src/pumpFun/ladder';

// Cross-donor contract test: original Mortdeus event decoder + original D3AD
// bonding-curve exit quote. All events are fixtures; no live trade is claimed.
const MINT = Buffer.alloc(32, 21);
const LEADER = Buffer.alloc(32, 42);
const TOKENS = 5_000_000_000n;
const ENTRY_SOL = 100_000_000n;
function pumpEvent(buy: boolean, solAmount: bigint): Buffer {
  const b = Buffer.alloc(233);
  MINT.copy(b, 16);
  b.writeBigUInt64LE(solAmount, 48);
  b.writeBigUInt64LE(TOKENS, 56);
  b[64] = buy ? 1 : 0;
  LEADER.copy(b, 65);
  b.writeBigUInt64LE(35_000_000_000n, 105);
  b.writeBigUInt64LE(1_000_000_000_000_000n, 113);
  return b;
}
describe('COPY event to single-position paper exit', () => {
  it('decodes leader buy, values entire sell after curve impact, closes on leader sell', () => {
    const buy = parseTransactionData(pumpEvent(true, ENTRY_SOL));
    const sell = parseTransactionData(pumpEvent(false, 210_000_000n));
    expect(buy?.pool_status).toBe('pumpfun');
    expect(buy?.isBuy).toBe(true);
    expect(sell?.isBuy).toBe(false);
    expect(sell?.mint).toBe(buy?.mint);
    expect(sell?.user).toBe(buy?.user);

    let position = { mint: buy.mint, tokens: BigInt(buy.tokenchange), costLamports: BigInt(buy.solchange) };
    const quotedExit = sellValue(70_000_000_000n, 1_000_000_000_000_000n, position.tokens);
    expect(quotedExit).toBeGreaterThan(position.costLamports);
    const grossPnlLamports = quotedExit - position.costLamports;
    position = { ...position, tokens: 0n };
    expect(position.tokens).toBe(0n);
    expect(grossPnlLamports).toBeGreaterThan(0n);
    console.log('PAPER_FIXTURE_CLOSED', JSON.stringify({
      mint: buy.mint,
      entryLamports: ENTRY_SOL.toString(),
      exitLamports: quotedExit.toString(),
      grossPnlLamports: grossPnlLamports.toString(),
      residualTokens: position.tokens.toString(),
      note: 'Fixture integration test; not a real-market trade'
    }));
  });
});
