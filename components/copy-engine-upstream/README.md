# 🚀 Solana Auto Trading Bot

<div align="center">

![Solana](https://img.shields.io/badge/Solana-9945FF?style=for-the-badge&logo=solana&logoColor=white)
![Rust](https://img.shields.io/badge/Rust-000000?style=for-the-badge&logo=rust&logoColor=white)
![WebSocket](https://img.shields.io/badge/WebSocket-010101?style=for-the-badge&logo=socket.io&logoColor=white)
![Helius](https://img.shields.io/badge/Helius-FF6B35?style=for-the-badge&logo=helius&logoColor=white)

**Advanced automated copy-trading bot for Solana — Helius WebSocket feed, multi-DEX mirroring, and an agent orchestrator with optional LLM + human-in-the-loop approval**

[Features](#-features) • [Installation](#-installation) • [Configuration](#-configuration) • [Usage](#-usage) • [API Reference](#-api-reference) • [Contributing](#-contributing)

</div>
<div align="center">
  <img width="756" height="321" alt="2025-09-10_13-23-31" src="https://github.com/user-attachments/assets/846541a9-41cb-4355-a0bb-ce1b3359e195" />
</div>

---

## ✨ Features

### Core Trading Features
- **Real-time Token Detection**: Monitors Helius WebSocket for swaps from `TARGET_PUBKEY`
- **Multi-DEX Support**: Pump.fun first, Raydium AMM v4 fallback via `mirror_swap`
- **Copy Trading**: Mirrors detected buy/sell activity through the agent policy layer
- **Risk Management**: Stop-loss, take-profit, trailing stop, and per-mint SL overrides
- **Position Tracking**: Entry SOL, peak value, token balance, and open position count; optional SQLite persistence
- **Multi-token Support**: Configurable max concurrent open positions

### Advanced Features
- **Nozomi Integration**: Optional tx backend via `TX_BACKEND=nozomi`
- **Zero Slot Support**: Optional tx backend via `TX_BACKEND=zeroslot`
- **Telegram Notifications**: Real-time alerts for trades and auto-exits (optional)
- **Jito Integration**: Bundle submission via `TX_BACKEND=jito`
- **bloXroute Support**: Alternative tx backend via `TX_BACKEND=bloxroute`
- **Agent Layer**: Event-bus orchestrator with rules policy, optional LLM tool-calling, and Telegram HITL approval
- **Dry Run Mode**: `DRY_RUN=true` logs decisions without submitting transactions
- **Telegram Control Plane**: Commands `/status`, `/positions`, `/pnl`, `/market`, `/pause`, `/resume`, `/config` + HITL approval buttons
- **Market Intelligence**: Per-mint venue, liquidity, supply, top-10 holder concentration, risk flags fed to LLM + `/market` command
- **PnL Tracking**: Realized PnL from closed trades in audit SQLite; exposed via `/pnl` + Telegram
- **Circuit Breaker**: Auto-pause after consecutive swap failures (`CIRCUIT_BREAKER_MAX_FAILURES`)

### Safety & Security
- **Slippage Protection**: Configurable slippage tolerance per swap
- **Position Limits**: Caps concurrent open trades via `MAX_CONCURRENT_TRADES`
- **Error Handling**: Graceful WS reconnect loop, ctrl-c/SIGTERM shutdown, and per-trade error logging
- **Tx Backend Fallback**: `TX_BACKEND_FALLBACK` when primary sender fails
- **Mint Filters**: Whitelist/blacklist + minimum target buy size via env
- **Global Pause**: `/pause` or circuit breaker stops new mirrors; exits still run

### Monitoring & Analytics
- **Real-time Logging**: Configurable levels via `tracing` (`RUST_LOG`)
- **Position State**: Open positions, entry SOL, peak value, and exit reason logging
- **Audit Log**: SQLite decision trail at `AGENT_AUDIT_DB_PATH`
- **HTTP Status**: Poll `/status` + `/pnl` for full bot report with market intel per position
- **Transaction Tracking**: Signature logging on successful mirror and auto-exit trades
- **Telegram Alerts**: Optional push notifications on trade events

### Technical Features
- **Rust Performance**: High-performance, memory-safe implementation
- **Modular Architecture**: Separate agent, engine, dex, core, and services layers
- **WebSocket Reconnection**: Reconnect loop with delay on disconnect
- **Multi-backend Tx Sender**: Unified `TxSender` for RPC, Jito, Nozomi, ZeroSlot, bloXroute
- **Async/Await**: Full async Tokio runtime
- **CI**: GitHub Actions runs `cargo check --all-targets` + `cargo test` on push/PR

### Implementation Status

- [x] Raydium AMM v4 swap
- [x] Pump.fun bonding curve swap
- [x] Multi-backend tx sender
- [x] WebSocket reconnect loop
- [x] Telegram notifications
- [x] Position tracker + SQLite persistence
- [x] Stop-loss / take-profit exit monitor
- [x] Agent layer (event bus + orchestrator + rules policy)
- [x] Optional LLM tool-calling layer
- [x] Telegram HITL approval (`AGENT_MODE=approve`)
- [x] Trailing stop + per-mint stop-loss overrides
- [x] Swap retry loop with buy verification
- [x] HTTP `/status` + `/health` endpoints
- [x] Agent audit log (SQLite)
- [x] Mint whitelist/blacklist filters
- [x] Dry-run mode
- [x] Telegram command bot (`/status`, `/pause`, `/positions`, `/pnl`, `/market`)
- [x] Rich market snapshot + risk flags for LLM
- [x] Realized PnL tracking + `/pnl` endpoint
- [x] Global pause + circuit breaker
- [x] Integration tests (WS fixture + agent pipeline)
- [x] Graceful shutdown (ctrl-c / SIGTERM)
- [x] On-chain buy verification after swap
- [x] Exit monitor publishes `ExitSignal` events to agent bus
- [x] Learning throttle (skips buys when win rate is poor)
- [x] Jito tip stream percentile (`JITO_TIP_STREAM_URL` + `JITO_TIP_PERCENTILE`)
- [x] Devnet profile (`AGENT_NETWORK=devnet`) + ignored integration test

---

## 🚀 Installation

### Prerequisites

- **Rust** (latest stable version)
- **Cargo** (comes with Rust)
- **Solana Wallet** with SOL for trading
- **Helius API Key** (for WebSocket feeds; other RPC providers work if they support `transactionSubscribe`)
- **Telegram Bot** (optional, for notifications)

### Quick Start

1. **Clone the repository**
   ```bash
   git clone https://github.com/0xJonaseb11/solana-trading-bot.git
   cd solana-trading-bot
   ```

2. **Install Rust** (if not already installed)
   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   source ~/.cargo/env
   ```

3. **Set up environment variables**
   ```bash
   cp .env.example .env
   ```

4. **Configure your environment**
   Edit `.env` — see [Configuration](#-configuration) for the full list. Minimum required:
   ```env
   PRIVATE_KEY=your_base58_private_key
   SOL_PUBKEY=your_solana_public_key_here
   RPC_ENDPOINT=your_helius_rpc_endpoint
   RPC_WEBSOCKET_ENDPOINT=your_helius_websocket_endpoint
   TARGET_PUBKEY=target_wallet_to_monitor
   JUP_PUBKEY=jupiter_aggregator_pubkey
   JITO_BLOCK_ENGINE_URL=your_jito_url   # when TX_BACKEND=jito
   ```

5. **Build and run**
   ```bash
   # Development
   cargo run

   # Release build
   cargo build --release
   RUST_LOG=info ./target/release/trading-bot
   ```

---

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `PRIVATE_KEY` | Base58 wallet private key | — | ✅ |
| `SOL_PUBKEY` | Wrapped SOL mint pubkey | — | ✅ |
| `RPC_ENDPOINT` | HTTPS RPC endpoint | — | ✅ |
| `RPC_WEBSOCKET_ENDPOINT` | WSS endpoint for tx subscribe | — | ✅ |
| `TARGET_PUBKEY` | Target wallet to copy | — | ✅ |
| `JUP_PUBKEY` | Jupiter pubkey to exclude from feed | — | ✅ |
| `SLIPPAGE` | Slippage percent | `5` | ❌ |
| `BUY_AMOUNT_SOL` | Max SOL per mirrored buy | `0.01` | ❌ |
| `MAX_CONCURRENT_TRADES` | Max open positions | `5` | ❌ |
| `STOP_LOSS_PERCENTAGE` | Auto-sell when down this % | `20` | ❌ |
| `TAKE_PROFIT_PERCENTAGE` | Auto-sell when up this % | `50` | ❌ |
| `TRAILING_STOP_PERCENTAGE` | Auto-sell when price drops this % from peak (`0` = off) | `0` | ❌ |
| `MINT_STOP_LOSS_OVERRIDES` | Per-mint SL, e.g. `mint1:15,mint2:20` | — | ❌ |
| `POSITION_POLL_SECS` | Exit monitor poll interval (seconds) | `5` | ❌ |
| `POSITIONS_DB_PATH` | SQLite path; empty disables persistence | `positions.db` | ❌ |
| `DRY_RUN` | Log-only mode, no txs | `false` | ❌ |
| `TX_BACKEND` | `rpc`, `jito`, `nozomi`, `zeroslot`, `bloxroute` | `jito` | ❌ |
| `TX_BACKEND_FALLBACK` | Fallback tx sender | `rpc` | ❌ |
| `USE_JITO` | Legacy flag (prefer `TX_BACKEND`) | `true` | ❌ |
| `TX_SIMULATE` | Simulate txs before send | `false` | ❌ |
| `UNIT_PRICE` | Compute unit price (micro-lamports) | `20000` | ❌ |
| `UNIT_LIMIT` | Compute unit limit | `200000` | ❌ |
| `AGENT_MODE` | `rules`, `llm`, or `approve` | `rules` | ❌ |
| `AGENT_AUTO_APPROVE` | Skip Telegram buttons in approve mode | `true` | ❌ |
| `AGENT_LLM_IN_APPROVE` | Run LLM refinement in `approve` mode (in addition to rules) | `false` | ❌ |
| `AGENT_NETWORK` | `mainnet` or `devnet` (relaxes RPC URL validation on devnet) | `mainnet` | ❌ |
| `AGENT_APPROVAL_TIMEOUT_SECS` | HITL approval timeout (seconds) | `60` | ❌ |
| `AGENT_MAX_RETRIES` | Swap retry attempts with backoff | `3` | ❌ |
| `AGENT_AUDIT_DB_PATH` | SQLite path for agent decision audit | `agent_audit.db` | ❌ |
| `MINT_WHITELIST` | Comma-separated allowed mints (empty = all) | — | ❌ |
| `MINT_BLACKLIST` | Comma-separated blocked mints | — | ❌ |
| `MIN_TARGET_BUY_SOL` | Ignore target buys below this SOL size | `0` | ❌ |
| `COPY_SELLS` | Mirror target sell events | `true` | ❌ |
| `PORT` | HTTP port on Render (falls back to `STATUS_PORT`) | — | ❌ |
| `STATUS_PORT` | HTTP status server port (local default) | `8080` | ❌ |
| `CIRCUIT_BREAKER_MAX_FAILURES` | Auto-pause after N consecutive swap errors | `5` | ❌ |
| `LLM_API_URL` | OpenAI-compatible chat completions URL | — | ❌ |
| `LLM_API_KEY` | LLM API key | — | ❌ |
| `LLM_MODEL` | LLM model name | `gpt-4o-mini` | ❌ |
| `JITO_BLOCK_ENGINE_URL` | Jito block engine URL | — | ❌* |
| `JITO_TIP_VALUE` | Jito tip in SOL (fallback when tip stream unavailable) | `0.001` | ❌ |
| `JITO_TIP_STREAM_URL` | Jito tip stream WebSocket for dynamic tips | — | ❌ |
| `JITO_TIP_PERCENTILE` | Percentile from tip stream (e.g. `50` = median) | `50` | ❌ |
| `NOZOMI_URL` | Nozomi endpoint | — | ❌ |
| `NOZOMI_TIP_VALUE` | Nozomi tip in SOL | `0.001` | ❌ |
| `ZERO_SLOT_URL` | ZeroSlot endpoint | — | ❌ |
| `ZERO_SLOT_TIP_VALUE` | ZeroSlot tip in SOL | `0.001` | ❌ |
| `BLOXROUTE_URL` | bloXroute submit URL | — | ❌ |
| `BLOXROUTE_AUTH` | bloXroute auth header | — | ❌ |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | — | ❌ |
| `TELEGRAM_CHAT_ID` | Telegram chat ID | — | ❌ |

\* Required when `TX_BACKEND=jito`.

See `.env.example` for Raydium pool keys (`RAY_PUBKEY`, `RAY_AUTHORITY_V4`, `WSOL_USDC`) used by the Raydium integration.

### Agent Modes

| Mode | Behavior |
|------|----------|
| `rules` | Policy decides mirror buy/sell; no LLM, no approval gate |
| `llm` | Policy proposes action; LLM may refine via tool-calling (`mirror_buy`, `mirror_sell`, `skip`) |
| `approve` | Rules policy only; non-skip trades require Telegram approval unless `AGENT_AUTO_APPROVE=true` |

`AGENT_MODE` is one of `rules`, `llm`, or `approve` — not combinable. Use `llm` for LLM refinement; use `approve` for human-in-the-loop without LLM. Set `AGENT_LLM_IN_APPROVE=true` to also run the LLM in approve mode before the Telegram gate.

In `approve` mode with `AGENT_AUTO_APPROVE=false`, the bot sends inline **Approve / Reject** buttons. A background poller resolves callbacks; trades skip on timeout (`AGENT_APPROVAL_TIMEOUT_SECS`).

The policy layer applies a **learning throttle**: after 5+ closed trades, buys are skipped when realized win rate falls below 35%.

### Trading Configuration

Defaults in `src/common/constants.rs`, overridable via env:

```rust
pub const BUY_AMOUNT_SOL: f64 = 0.01;
pub const MAX_CONCURRENT_TRADES: usize = 5;
pub const STOP_LOSS_PERCENTAGE: f64 = 20.0;
pub const TAKE_PROFIT_PERCENTAGE: f64 = 50.0;
```

### DEX Routing

- **Pump.fun**: Tried first for every mirror swap
- **Raydium**: Fallback when Pump.fun swap fails (pool resolved by mint); fallback is logged at `warn` level
- **Not Jupiter routing**: The bot mirrors swaps; it does not route through Jupiter aggregator

---

## 🎯 Usage

### Basic Usage

```bash
# Start the bot in development mode
cargo run

# Build and run in release mode
cargo build --release
RUST_LOG=info ./target/release/trading-bot
```

### Dry Run

Validate WebSocket feed + parser without submitting transactions:

```bash
DRY_RUN=true RUST_LOG=info ./target/release/trading-bot
```

### Human-in-the-Loop (Approve Mode)

```bash
AGENT_MODE=approve AGENT_AUTO_APPROVE=false cargo run
```

Requires `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`. Trades pause until you tap Approve or Reject in Telegram.

### Telegram Commands

When Telegram is configured, send these in your chat:

| Command | Description |
|---------|-------------|
| `/status` | Bot state, wallet SOL, pause flag, PnL headline |
| `/positions` | Open positions with live quotes + risk flags |
| `/pnl` | Realized PnL, win rate, decision stats |
| `/market <mint>` | Venue, liquidity, supply, top-10 holder %, risk flags for a mint |
| `/config` | SL/TP/trailing thresholds + buy cap |
| `/target` | Monitored wallet pubkey |
| `/pause` | Stop new copy trades (exits still run) |
| `/resume` | Resume copy trading |
| `/help` | Command list |

### Status Endpoint

The bot exposes a lightweight HTTP server on `PORT` (Render) or `STATUS_PORT` (default `8080`):

```bash
curl http://localhost:8080/health
curl http://localhost:8080/status
curl http://localhost:8080/pnl
```

`/status` returns target pubkey, agent mode, pause state, wallet SOL, open positions with market intel, and PnL summary.

### Runtime Flow

1. **Connects to WebSocket** and subscribes to target wallet transactions
2. **Parses swaps** via `ray_parse` (Raydium / Pump.fun transfers)
3. **Publishes events** to the agent event bus (`DetectedSwap`, `PositionValued`)
4. **Orchestrator** runs: snapshot → policy → optional LLM → optional Telegram approval → retry loop
5. **Executes** Pump.fun swap first, Raydium fallback
6. **Exit monitor** polls pricing, tracks peak value, and publishes `PositionValued` for SL/TP/trailing exits
7. **Notifies** Telegram when configured; **records** decisions to audit SQLite

### Logging

```bash
RUST_LOG=debug cargo run
RUST_LOG=info cargo run
```

### Monitoring

1. **Console Logs** — structured output via `tracing`
2. **Telegram Notifications** — trade alerts, auto-exits, and HITL approval buttons (optional)
3. **HTTP `/status`** — live snapshot of bot state and open positions
4. **SQLite** — open positions at `POSITIONS_DB_PATH`; agent audit at `AGENT_AUDIT_DB_PATH`
5. **Transaction Signatures** — logged on successful mirror and exit trades

---

## 📊 API Reference

### Core Modules

#### Trading Engine (`src/engine/`)

| File | Purpose |
|------|---------|
| `strategy.rs` | `TradeConfig`, SL/TP/trailing/per-mint SL, `AgentMode`, env parsing |
| `sniper.rs` | `mirror_swap` router (Pump.fun → Raydium) |
| `swap.rs` | `pump_swap` / `raydium_swap` wrappers |
| `positions.rs` | In-memory position tracker |
| `position_store.rs` | SQLite persistence |
| `pricing.rs` | Pump + Raydium sell quotes |
| `exit_monitor.rs` | Background valuation loop → `PositionValued` events |

#### Agent (`src/agent/`)

| File | Purpose |
|------|---------|
| `bus.rs` | `EventBus` + orchestrator loop |
| `context.rs` | `AgentContext` (positions, config, audit, approval) |
| `events.rs` | `TradeEvent` / `TradeAction` |
| `policy.rs` | Rules-based decisions + mint filters |
| `runner.rs` | `process_event` pipeline (policy → LLM → approval → retry) |
| `llm.rs` | Optional LLM tool-calling layer |
| `tools.rs` | Tool registry (`mirror_buy`, `mirror_sell`, `skip`) |
| `snapshot.rs` | Market + position snapshot for LLM context |
| `audit.rs` | SQLite audit log (decisions + target swaps) |
| `approval.rs` | Telegram HITL approval service |
| `bot_status.rs` | Unified status/PnL report for HTTP + Telegram |
| `control.rs` | Global pause + circuit breaker |
| `market.rs` | Per-mint venue, liquidity, risk flags |
| `planner.rs` | Swap retry loop + buy verification |

#### DEX (`src/dex/`)

| File | Purpose |
|------|---------|
| `pump_fun.rs` | Pump.fun bonding curve swaps |
| `raydium.rs` | Raydium AMM v4 swaps |

#### Services (`src/services/`)

| File | Purpose |
|------|---------|
| `jito.rs` | Jito bundle submission |
| `nozomi.rs` | Nozomi tx sender |
| `zeroslot.rs` | ZeroSlot tx sender |
| `bloxroute.rs` | bloXroute tx sender |
| `telegram.rs` | Notifications + Telegram HTTP API |
| `telegram_bot.rs` | Command handler + approval callback poller |
| `status_server.rs` | Axum `GET /health`, `/status`, `/pnl` |
| `rpc_client.rs` | RPC helpers |

#### Other

| Path | Purpose |
|------|---------|
| `src/core/tx_sender.rs` | Multi-backend tx dispatch |
| `src/core/balance.rs` | ATA token balance fetch |
| `src/ray_parse/tx_parse.rs` | WebSocket swap parser |
| `src/error.rs` | Shared error types |

### Key Functions

```rust
// Mirror swap — Pump.fun first, Raydium fallback
pub async fn mirror_swap(
    state: AppState,
    mint: String,
    amount_in: f64,
    direction: &str,
    config: &TradeConfig,
) -> Result<Vec<String>>

// Agent pipeline (WS + exit monitor → event bus → orchestrator)
pub async fn process_event(
    ctx: &AgentContext,
    event: TradeEvent,
) -> Result<()>
```

---

## 🔧 Development

### Project Structure

```
src/
├── lib.rs                  # Library entry (crate: copy_trading_bot)
├── main.rs                 # Binary entry (trading-bot)
├── error.rs                # Error types
├── agent/
│   ├── approval.rs
│   ├── audit.rs
│   ├── bot_status.rs
│   ├── bus.rs
│   ├── context.rs
│   ├── control.rs
│   ├── events.rs
│   ├── llm.rs
│   ├── market.rs
│   ├── mod.rs
│   ├── planner.rs
│   ├── policy.rs
│   ├── runner.rs
│   ├── snapshot.rs
│   └── tools.rs
├── common/
│   ├── cache.rs
│   ├── constants.rs
│   ├── logger.rs
│   ├── mod.rs
│   └── utils.rs
├── core/
│   ├── balance.rs
│   ├── mod.rs
│   ├── token.rs
│   ├── tx.rs
│   └── tx_sender.rs
├── dex/
│   ├── mod.rs
│   ├── pump_fun.rs
│   └── raydium.rs
├── engine/
│   ├── exit_monitor.rs
│   ├── mod.rs
│   ├── position_store.rs
│   ├── positions.rs
│   ├── pricing.rs
│   ├── sniper.rs
│   ├── strategy.rs
│   └── swap.rs
├── ray_parse/
│   ├── mod.rs
│   └── tx_parse.rs
└── services/
    ├── bloxroute.rs
    ├── jito.rs
    ├── mod.rs
    ├── nozomi.rs
    ├── rpc_client.rs
    ├── status_server.rs
    ├── telegram.rs
    ├── telegram_bot.rs
    └── zeroslot.rs
```

### Building

```bash
cargo build
cargo build --release
cargo clean
cargo check
```

### Testing

```bash
cargo test --lib
cargo test --lib -- --nocapture
```

CI: `.github/workflows/ci.yml` — runs `cargo check --all-targets` and full `cargo test` (lib + integration).

### Deploy (Render)

A starter [`render.yaml`](render.yaml) is included. Bind secrets in the Render dashboard (`RPC_ENDPOINT`, `PRIVATE_KEY`, etc.). **Note:** Render's filesystem is ephemeral — SQLite DBs reset on redeploy unless you attach a [Render Disk](https://render.com/docs/disks).

```bash
# Health check after deploy
curl https://your-service.onrender.com/health
```

---

## 🛡️ Security Considerations

### Wallet Security

- **Never commit private keys** to version control
- **Use environment variables** for sensitive data
- **Consider using a hardware wallet** for large amounts
- **Regularly rotate keys** and monitor transactions

### Trading Risks

- **Start with small amounts** to test the bot
- **Monitor performance** regularly
- **Set appropriate stop-losses** to limit downside
- **Understand the risks** of automated trading

### Best Practices

- **Use `DRY_RUN=true`** to validate WS feed before going live
- **Monitor logs** for errors and anomalies
- **Keep the bot updated** with latest changes
- **Backup your configuration** regularly

---

## 📈 Performance Tips

1. **Use a fast RPC endpoint** — parsing and pricing hit RPC on every detected swap
2. **Tune `POSITION_POLL_SECS`** — lower = faster exits, more RPC load
3. **Pick the right `TX_BACKEND`** for your latency vs cost tradeoff
4. **Set `MAX_CONCURRENT_TRADES`** to match your capital and RPC limits

---

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit format, and PR workflow.

Quick start:

```bash
git checkout main && git pull
git checkout -b feat/holders
cargo test
git push -u origin feat/holders
```

---

## 📝 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

## ⚠️ Disclaimer

**This software is for educational purposes only. Trading cryptocurrencies involves substantial risk of loss and is not suitable for all investors. The high degree of leverage can work against you as well as for you. Before deciding to trade cryptocurrencies, you should carefully consider your investment objectives, level of experience, and risk appetite. The possibility exists that you could sustain a loss of some or all of your initial investment and therefore you should not invest money that you cannot afford to lose. You should be aware of all the risks associated with cryptocurrency trading and seek advice from an independent financial advisor if you have any doubts.**

---

## 🆘 Support

### Getting Help

- **Issues**: [GitHub Issues](https://github.com/0xJonaseb11/solana-trading-bot/issues)
- **Discussions**: [GitHub Discussions](https://github.com/0xJonaseb11/solana-trading-bot/discussions)

### Common Issues

1. **Connection Issues** — Check RPC/WSS endpoints and network
2. **Transaction Failures** — Verify wallet SOL balance, slippage, and tx backend config
3. **WebSocket Disconnections** — Bot reconnects automatically after 3 seconds
4. **No mirrors firing** — Confirm `TARGET_PUBKEY` activity hits Pump.fun or Raydium programs in the subscription filter
5. **Approval stuck** — Ensure Telegram bot token/chat ID are set and the poller is running (`AGENT_MODE=approve`, `AGENT_AUTO_APPROVE=false`)
6. **Status endpoint unreachable** — Check `PORT` / `STATUS_PORT`; bot binds `0.0.0.0`

---

<div align="center">

**Made with ❤️ for the Solana community**

[⭐ Star this repo](https://github.com/0xJonaseb11/solana-trading-bot) • [🐛 Report Bug](https://github.com/0xJonaseb11/solana-trading-bot/issues) • [💡 Request Feature](https://github.com/0xJonaseb11/solana-trading-bot/issues)

</div>
