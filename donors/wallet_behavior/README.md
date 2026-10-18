# copytrade — measuring whether "smart money" wallets are actually profitable

A Solana wallet-monitoring system that reconstructs the complete trading activity
of a set of addresses directly from on-chain data, mirrors that activity into a
simulated portfolio, and reports the realised outcome per address. It reads
transactions from the ledger, derives what each address bought, sold or moved
out, and keeps a full audit trail of every decision it made and why.

The results published here come from the simulation mode. The codebase also
contains a live execution path; it is disabled by default and was not used to
produce any of the numbers below.

Russian-language technical documentation, covering every configuration key and
design decision in detail, is in [README.ru.md](README.ru.md).

---

## Problem

A large informal industry publishes lists of "smart money" Solana wallets —
addresses said to consistently front-run retail into new tokens. The claim is
easy to make and rarely tested, because testing it requires reconstructing what
an address actually did, not what a dashboard says it did.

The question I set out to answer was narrow and falsifiable:

> If you had copied these specific addresses, trade for trade, over a defined
> period, what would the outcome have been — and how much of that outcome is
> attributable to skill rather than to a handful of outliers?

The secondary question turned out to be the more interesting one, and it is the
part relevant to transaction-monitoring work:

> What does it take to read an address's behaviour correctly from raw ledger
> data? Where do naive readings go wrong, and what is the size of the error?

The two questions are linked. Any measurement of an address's performance is
only as good as the classification underneath it. If a transfer to another
address is scored as a sale, the address looks profitable when it has merely
moved inventory. If a swap routed through an aggregator the parser does not
recognise, the trade is invisible. Both failure modes produce a clean-looking
number that is wrong.

---

## How it works

**Addresses are watched continuously, with two independent paths.** A WebSocket
subscription delivers transactions as they are confirmed. A polling loop
independently re-reads each address's signature history from the last processed
point. The subscription is faster; the poller is the one that is correct.
Solana's `logsSubscribe` offers no delivery guarantee, so a system that trusts
it alone silently loses transactions and never reports the loss. Each address
carries its own read cursor, because the underlying RPC pagination works per
address.

**Activity is derived from balance changes, not from recognised programs.** For
each transaction, the system computes the net change in the address's token
balances and lamport balance, before and after. It does not attempt to identify
which exchange or router was used. This is the central design decision: Jupiter,
Raydium, pump.fun, PumpSwap, Meteora and any venue launched next month all leave
different instructions but the same balance footprint. A parser built on program
recognition needs updating every time a new venue appears; a parser built on
balance deltas does not.

**Movements are then classified by what was exchanged for what:**

| Observed | Classified as |
|---|---|
| Quote currency out, token in | Buy |
| Token out, quote currency in | Sell |
| Token out, nothing in | Transfer out |
| Token in, nothing paid | Transfer in (airdrop or incoming) |
| SOL to wrapped SOL and back | Not a trade; ignored |

The distinction between a sale and a transfer out matters more than it appears.
Both reduce the address's balance to zero. Only one of them realises value. An
address that routinely moves tokens to a second address it controls will, under
a naive reading, appear to be closing positions at market price.

**Exits are mirrored as a proportion, not an amount.** When a watched address
sells, the system reads the fraction of its remaining balance that was disposed
of — computed from the balances inside that same transaction — and applies that
fraction to the simulated holding. This is cumulative by construction: if the
address bought more without the system noticing, or if an intermediate
transaction was missed entirely, the proportion read from the next transaction
is still correct. An absolute-quantity approach would drift permanently after a
single gap.

**When several watched addresses hold the same token, one is designated the
lead.** Only that address's exits govern the simulated position. Precedence is
decided by position in the ledger — slot, then block time — and never by the
order in which the system happened to process the data. After a reconnection,
history arrives in batches and out of order; ordering by arrival would credit
the wrong address with having moved first. The system detects a late-arriving
earlier purchase and reassigns attribution, while leaving the recorded entry
price untouched, since that entry was genuinely executed at that price.

**Every decision is recorded with its reason.** Skipped trades are logged with a
rule code — below the size threshold, position already held, no reliable price,
signal too old — alongside the transaction signature. The count of what was
rejected, and on what grounds, is part of the output rather than a side effect.

---

## Data

**Sources.** Solana JSON-RPC for ledger data (`getSignaturesForAddress`,
`getTransaction` in parsed form, `getTokenAccountsByOwner`). Jupiter Price API
for token valuation and symbol resolution. CoinGecko and Binance as independent
fallbacks for the SOL/USD reference rate, so that a single price source going
down does not stall or distort the record.

**Unit of observation.** One transaction, one address, one token. For each, the
system stores the balance before, the balance after, the quote currency involved
and the amount, whether the price is exact (derivable from the transaction
itself) or estimated (obtained from a price API), the slot, and the block time.

**Derived records.** Trade events; simulated positions with their full history
of partial exits; per-address counters for detected, copied and rejected
activity; and a resumable state file that lets a run survive a restart without
double-counting or gaps.

**Outputs.** A newline-delimited JSON event stream, one object per event, for
machine reading. Three CSV extracts — per position, per exit, per address.
A plain-text summary, rewritten atomically so an external reader never sees a
half-written file. A JSON state file with an atomic-replace write and a backup
copy, since an interrupted write would otherwise destroy the run's history.

**Observation run reported below.** Ten addresses, monitored continuously for
approximately 52 hours in late July 2026. 3,544 transactions parsed with zero
parse failures. 1,513 buys and 1,288 sells detected; 238 transfer-out events.
724 positions opened at a fixed simulated size of 0.1 SOL each; 699 closed.

**Data quality.** Of 1,163 mirrored exits, 1,057 (91%) were priced directly from
the transaction that triggered them, 91 (8%) from the price API, and 2 from the
last known price. Only the first category is exact. The proportion is reported
because the second and third categories carry error that the headline number
does not show.

Raw run data is not included in this repository. It contains the addresses under
observation and the labels assigned to them.

---

## What it surfaces

**The median trade lost money; the aggregate did not.** Across 699 closed
positions, 217 were profitable and 482 were not. The median position returned
−0.0095 SOL. Total realised return was +2.00 SOL on a 69.9 SOL cost basis, or
+2.87%. The positive aggregate is produced entirely by the right tail: the five
best positions account for 27.7% of all gross gains, and the single best
position for 9.6%. Removing the top five turns the result negative.

This is the substantive finding. A win rate of 31% is not a defect of the
addresses being copied — it is the shape of the distribution they operate in.
Any measure that reports hit rate, or average return, without the distribution
behind it will mislead in both directions.

**Sample size explains most of the apparent ranking.** Seven of the ten
addresses finished net positive and three net negative, with returns spanning
+58% to −25%. The best return by percentage came from an address with 18 closed
positions. The address with 230 positions returned +3.3%. The high-percentage,
low-count results are not distinguishable from noise at this sample size, and
the ranking a naive leaderboard would produce is largely an artefact of that.

**Holding periods are not investment periods.** Median holding time for a closed
position was 37 seconds. The mean was 17 minutes, pulled up by a small number of
positions held for hours. 698 of 699 closures were triggered by the watched
address fully exiting, not by any independent decision.

**Most detected activity is not actionable.** 789 of 1,513 detected purchases —
52% — were rejected before execution by explicit rules, mostly for falling below
the size threshold or duplicating an existing position. The raw signal "this
address bought something" has a low ratio of relevant to irrelevant events, and
the filtering rules, not the detection, do most of the work.

**Transfers out are common and materially change attribution.** 238 events were
token movements out of a watched address with no consideration received. Under
the default configuration these do not close a simulated position, on the
grounds that inventory moved is not value realised. Configured the other way,
each would be scored as a sale at the prevailing market price. The same ledger,
read two ways, produces two different performance figures for the same address.

**Ordering defects are silent unless you look for them.** The system tracks a
counter for cases where a late-arriving transaction proves an earlier
attribution wrong. In this run the counter was zero — the mechanism did not
fire. It is reported here because a zero that was measured is a different claim
from a zero that was assumed, and the same reasoning applies to the parse-failure
count.

---

## Limitations

This section is the important one. The method has real boundaries, and several
of the numbers above would not survive contact with live execution.

**Simulation is not execution.** Fills are assumed at the price the watched
address itself obtained. In reality a copying order arrives after the address's
order, into a market its order has already moved. On low-liquidity tokens with a
37-second median holding period, this is not a rounding error — it is plausibly
larger than the entire +2.87% result. The simulation applies fixed fees but, by
default, does not model slippage at all, because the configured slippage value
is a tolerance rather than an expected loss. Market impact, sandwich attacks,
failed transactions, partial fills and priority-fee competition are all absent.
**The reported return should be read as an upper bound that live execution would
not reach, not as a forecast.**

**The addresses were selected, not sampled.** Ten addresses chosen by me,
partly because they already looked promising. Nothing here supports a claim
about "smart money wallets" as a population. The correct reading is: these ten
addresses, over these 52 hours, produced these outcomes.

**The observation window is short and regime-specific.** Roughly two days, in
one market (Solana memecoins), in one period. Distributions with tails this
heavy require far longer windows before per-address estimates stabilise. The
per-address figures — 9 to 230 positions each — are well below what would be
needed to separate skill from variance.

**Attribution identifies sequence, not control.** The "lead" designation records
which watched address transacted first in ledger order. It is not evidence of
coordination, common ownership, or any relationship between addresses. Two
addresses buying the same token in adjacent slots is fully consistent with two
independent parties reacting to the same public event. **This system does not
cluster addresses, does not resolve entities, and cannot establish control.**

**Transfer destinations are never resolved.** The system detects that tokens
left an address. It does not examine the receiving address, does not determine
whether it belongs to the same party, and does not distinguish a move between
one party's own wallets from a payment to a third party. All 238 transfer-out
events in this run are unresolved in that sense.

**Not all activity is visible to balance-delta parsing.** Movements that net to
zero within a single transaction leave no footprint and are not detected. NFT
activity, staking, lending, governance and non-SPL instruments are out of scope.
The system sees fungible token and SOL balance changes, and nothing else.

**Price data carries error, and the error is not uniform.** 8% of mirrored exits
were priced from an API at processing time rather than from the transaction, and
the gap between those two moments is not zero. Tokens too illiquid for the price
service to quote are simply absent, which biases the surviving sample toward
tokens that were liquid enough to be quoted.

**Delivery gaps are mitigated, not eliminated.** The poller backstops the
subscription, but a sufficiently long outage — beyond roughly ten thousand
signatures for an address — exceeds the catch-up pagination limit. The system
logs a warning when this happens; it does not recover the missing history.

**Fixed sizing measures a specific strategy, not the addresses' own returns.**
Every simulated position is 0.1 SOL regardless of how much the watched address
committed. The result therefore answers "what if you copied every qualifying
trade at equal weight", which is a different question from "what did these
addresses earn". An address that sizes up on its best ideas is penalised by this
method.

**This is not a compliance or investigative tool.** It contains no
identity data, no sanctions or watchlist screening, no exchange or service
attribution, and no risk scoring. It observes addresses, not people or entities.
**Nothing in this repository constitutes evidence of wrongdoing by any address,
and no output should be read as an allegation.** What it demonstrates is the
data-handling layer beneath that kind of work: parsing ledger activity into
classified events, distinguishing disposals from transfers, keeping attribution
honest under out-of-order delivery, and reporting what was rejected and why.

---

## Tech stack

Python 3.11+, no framework. Fully asynchronous on `asyncio`, with `aiohttp` for
HTTP and `websockets` for the subscription. No Solana SDK is used for reading
the chain — the RPC client is written directly against JSON-RPC, since only a
handful of methods are needed and the SDK would pull in native build
dependencies for no benefit. `solders` is imported lazily and only on the live
execution path, so the analysis path runs without it.

All monetary arithmetic uses `Decimal` at 40 digits of precision. Floating point
is not used anywhere in the value path: token prices reach 1e-12 SOL and
positions are tracked to the lamport, where binary floating point accumulates
visible error.

Storage is plain files by design — JSON for resumable state, JSONL for the event
stream, CSV for extracts. There is no database, because a single-writer
append-and-replace workload does not need one, and files remain readable by any
tool without a schema.

The parser is a pure function from transaction JSON to typed events, with no I/O,
which is what makes it testable against fixtures. Seven test modules cover
transaction classification, proportional exit logic, the small-disposal
threshold, lead attribution and reassignment, signal-freshness guards, risk
limits, and summary output. They run under `pytest` or standalone:

```bash
python tests/test_parser.py
```

Configuration is layered — a JSON file for defaults, environment variables
overriding it — with type coercion and validation at load time. Fields marked as
secret are rejected outright if found in the JSON file, so a private key or API
key cannot reach a committed file through that path.
