import fs from 'node:fs';
import path from 'node:path';
import { sellValue } from '../src/pumpFun/ladder';

/** No signer, RPC order submission, or wallet access. One paper position maximum. */
export type PaperPosition = {
  mint: string; leader: string; tokenAmount: string; entryLamports: string;
  openedAt: string; entrySignature: string;
};
export type PaperClose = {
  mint: string; leader: string; entryLamports: string; exitLamports: string;
  pnlLamports: string; exitSignature: string; closedAt: string; residualTokens: '0';
};
export type PaperSnapshot = { position: PaperPosition | null; closed: PaperClose[]; seenSignatures: string[] };
export class CopyPaperLedger {
  state: PaperSnapshot;
  constructor(readonly filename: string) {
    this.state = fs.existsSync(filename)
      ? JSON.parse(fs.readFileSync(filename, 'utf8')) as PaperSnapshot
      : { position: null, closed: [], seenSignatures: [] };
  }
  private persist(): void {
    fs.mkdirSync(path.dirname(this.filename), { recursive: true });
    const tmp = this.filename + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2));
    fs.renameSync(tmp, this.filename);
  }
  onBuy(event: { mint: string; leader: string; signature: string; tokens: bigint; costLamports: bigint }): boolean {
    if (this.state.seenSignatures.includes(event.signature) || this.state.position) return false;
    if (!event.mint || !event.leader || event.tokens <= 0n || event.costLamports <= 0n) return false;
    this.state.position = { mint: event.mint, leader: event.leader, tokenAmount: event.tokens.toString(),
      entryLamports: event.costLamports.toString(), openedAt: new Date().toISOString(),
      entrySignature: event.signature };
    this.state.seenSignatures.push(event.signature);
    this.persist();
    return true;
  }
  onSell(event: { mint: string; leader: string; signature: string; virtualSol: bigint;
    virtualTokens: bigint }): PaperClose | null {
    const p = this.state.position;
    if (!p || p.mint !== event.mint || p.leader !== event.leader ||
      this.state.seenSignatures.includes(event.signature)) return null;
    if (event.virtualSol <= 0n || event.virtualTokens <= 0n) return null;
    const exit = sellValue(event.virtualSol, event.virtualTokens, BigInt(p.tokenAmount));
    if (exit <= 0n) return null;
    const close: PaperClose = {
      mint: p.mint, leader: p.leader, entryLamports: p.entryLamports,
      exitLamports: exit.toString(),
      pnlLamports: (exit - BigInt(p.entryLamports)).toString(),
      exitSignature: event.signature, closedAt: new Date().toISOString(), residualTokens: '0'
    };
    this.state.closed.push(close);
    this.state.seenSignatures.push(event.signature);
    this.state.position = null;
    this.persist();
    return close;
  }
}
