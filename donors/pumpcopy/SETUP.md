# PumpCopy setup

Everything lives in `config.json` next to the exe and is edited from the app
only: change fields, press `save` at the bottom right of the tab, `reload`
pulls the last saved config back into the UI.

## Main tab

![Main](docs/screen-main.png)

- header: version, mode badge (PAPER / LIVE), state (running / stopped /
  startup error text), START / STOP
- stats strip: copied, skipped, errors, session pnl in SOL
- Leaders: who you copy. `on` = watch the wallet, alias = name for logs and
  feed, wallet = leader address (truncated), copied / last = copy count and
  last copy time. add opens a dialog (alias, full base58 address,
  multiplier: size scaling for copies of this leader), remove deletes the
  selected row
- Live feed: every leader trade and the bot reaction: time, leader, side
  (BUY aqua / SELL red), mint, leader SOL, our SOL, status. Status is the
  column that matters: `sent <sig>`, `paper buy ...`,
  `skipped: leader size ... < min`, `skipped: wallet sol ... < size + fees`,
  `skipped: position already open`, `buy error: ...` and so on
- Log: live log, the same lines go to `logs/pumpcopy-*.log`. autoscroll,
  clear. Colors: gray info, aqua leader/copy trades, yellow warn, red err,
  dark gray dbg (only with verbose on)
- status bar: rpc latency + active endpoint, ws state, open positions,
  session pnl

The feed status column and the log are the only source of truth about why a
copy happened or did not.

## Wallet tab

![Wallet](docs/screen-wallet.png)

- show keys: secrets are masked by default (`4roN****...****4pfi`). Turn it
  on only when you need to verify a key, and not with someone looking at
  your screen
- import .txt: file with one base58 secret (64 bytes, 88 chars) per line;
  solana-keygen json arrays `[12,34,...]` also work; blank lines and `#`
  comments are ignored
- add key: paste one key through a dialog
- remove: delete the selected row (keys.txt is rewritten on save)
- refresh balances: query chain, show SOL balance per wallet
- sell all: sell every pump token held by the selected trading wallet:
  pre-migration coins through the bonding curve (closes the ATA on a full
  exit), migrated ones through PumpSwap. Works with the bot stopped; while
  it runs, copying pauses for the dump and resumes after. Asks for
  confirmation
- trade (radio): which wallet trades. The rest just sit there, balances
  only. One trading wallet per bot
- save: rewrite `keys.txt` with the current list, store the trading wallet
  index in config

## Connection tab

![Connection](docs/screen-connection.png)

- RPC endpoints: the radio marks primary (the websocket connects through
  it), the rest form a pool for round-robin http calls and failover
  - `add`: dialog with the url, probes immediately (status/ms in the table)
  - `remove`: drop the selected row
  - `check all`: probe the whole list
  - `import list`: txt file, one url per line
  - what works: put public `https://api.mainnet-beta.solana.com` first,
    that is where the websocket lives, then 2-3 private keys (alchemy etc)
    for http load. Calls rotate across the pool so rate limits smear over
    endpoints. Alchemy ws does not work (subscriptions never confirm) and
    Infura does not serve Solana at all, do not waste time on either
- Proxy: use proxy + socks5 list. Line format (also for import):
  `socks5://host:port` or `socks5://user:pass@host:port`, one per line. The
  radio picks the active one; check probes it against the first rpc. The
  proxy covers both http and ws
- save: store the lists in config

Seeing "too many calls" or 429 in the log: add endpoints to the pool or put
a private rpc first. The websocket stays on endpoint #1 either way.

## Trading tab

![Trading](docs/screen-trading.png)

Left side:

- paper mode: simulated fills against the live curve, no keys needed.
  Always start here
- size mode: `fixed SOL`, or `follow leader` (leader SOL x multiplier x the
  leader's own multiplier)
- priority fee: micro-lamports per CU (low 50k / normal 100k / high 500k /
  turbo 1M). Raise it when the network is congested or your copies lag
- fixed SOL / multiplier / max our SOL: copy size and its cap
- slippage slider (%): price tolerance for buys, and for sells unless sell
  slippage is set
- max positions: how many different mints you hold at once; entries beyond
  that get skipped
- min leader SOL: ignore smaller leader trades (dust, wash)
- delay ms: random wait before copying
- sell slippage bps: separate tolerance for sells (default 1000 = 10%);
  sells move faster than buys, keep it looser
- below the fold (not in the screenshot): copy sells, skip migrated curves,
  allow repeat buys into an open position (averaging in with the leader),
  all off by default

Right side, logging:

- write log files: daily rotation into `logs/`
- level: dbg / info / warn / err
- demo feed: synthetic leader trades + virtual curves, paper mode only.
  Lets you watch the whole pipeline with no money and no network
- verbose debug logs: quotes, tx building (sizes, blockhash), send/poll
  status, every ws mention. Turn it on while debugging; every incident in
  the dev history was diagnosed from exactly this output

## Behavior worth knowing

- positions book optimistically at send time; on restart they come back
  from chain (`positions: reconstructed ...` in the log), reconstructed
  bags have no cost basis
- a failed sell retries after 1.5s with the full on-chain balance and
  double sell slippage (`retry sell ...` in the log)
- repeat leader buys into a mint you already hold are skipped unless repeat
  buys is on
- sell copies for mints you do not hold are ignored
  (`ignored: no open position in this mint`, dbg level)
- `skipped: wallet sol X < size + fees` is the balance gate: the wallet
  holds less than the copy size + 0.005 SOL fees. Fund it or lower the size
- migrated coins sell through PumpSwap; entries into them are controlled by
  skip migrated

## Files next to the exe

- `config.json`: all tab settings
- `keys.txt`: trading wallet secrets, one base58 per line
- `logs/pumpcopy-YYYYMMDD.log`: the log; `logs/crash.log`: rare crashes and
  unobserved exceptions (mostly socket teardown noise, harmless)
