# PumpCopy

Copy trade bot for shitcoin trades on [pump.fun](https://pump.fun/) (currently only Solana). It watches memecoin trades on whale addresses on-chain and mirrors their bonding-curve buys and sells from
your own wallet.

Built with:
- .NET8, WPF
- [Solnet](https://github.com/bmresearch/solnet) for RPC, websockets and tx building
- [Solgrid.Pumpfun](https://github.com/solgrid-development/Solgrid.Pumpfun) custom pump.fun SDK for v1/v2 bonding curves, PumpSwap, trade event decoding, routing.

> [!WARNING]
> **Its not stable strategy but can be profitable if you find really profitable address. 
> It can burn out your deposit or profit up to 5x by one transaction. 
> Also never use main wallets or high balances on this software.**

![PumpCopy](docs/screen-main.png)

## Features

Currently works:
- Tracks any number of leader (whale) wallets over websocket `logsSubscribe` and
  decodes pump.fun trade events from tx logs, so you copy actual fills, not
  instruction limits.
- Buys fixed SOL size, or proportional to the leader with a per-leader
  multiplier.
- Sells proportional to your remaining bag, if leader exits fully, your token
  account gets closed too.
- Filters min leader SOL, max our SOL, max open positions, skip migrated
  curves, random delay before copying.
- RPC pool with failover and round-robin, optional socks5 proxy for HTTP
  and WS.
- Live feed table with your trades, session stats, rolling file logs.

## Setup

Requirements:
- Developed and tested on Windows 10/11 x64
- Solana wallet with SOL for trades
- RPC endpoint (private Shyft or Quicknode will be better)
- Stable and fast internet connection (cloud VPS/VDS willl be better)
- optional: [.NET 8 runtime](https://dotnet.microsoft.com/download/dotnet/8.0) if you build from source

Setup release build:

1. Get binaries from [Releases](https://github.com/harutocodes/pumpfun-copytrade/releases), unpack anywhere.
2. Run `PumpCopy.App.exe`. First start writes `config.json` next to it.
3. Add your RPC endpoints in `Connection` tab, save.
4. Import your wallet key in `Wallet` tab, pick it as the trading wallet, save.
5. Uncheck paper mode in `Trading` tab, set size/slippage/priority fee, save.
6. Add whale accounts in Main tab, press START, watch the feed move.

> [!NOTE]  
> Tthe full per-tab guide with screenshots is in
[SETUP.md](SETUP.md).

From source (needs the SDK repo checked out next to this one):

    git clone https://github.com/harutocodes/pumpfun-copytrade.git
    dotnet run --project src/PumpCopy.App

## Some tips

- Keep paper mode and demo feed on first run. Watch how leader trades turn
  into copies, positions and pnl before you fund anything.
- Picking leaders: pump.fun leaderboards, birdeye/gmgn smart money lists,
  solscan top traders on tokens that actually ran. Good signs: early
  entries, staged sells, holds longer than 30 seconds, winrate above 50%
  over 20+ trades. Wallets that buy and dump in one tx are bundler noise,
  skip them.
- Min leader SOL keeps dust and wash trades out. 0.5 to 1 SOL is a sane
  mainnet default.
- Slippage: 3-5% on calm mints, 10-15% when copying fast wallets into thin
  curves. Priority fee high or turbo when the network is congested, or your
  copy lands two candles late.
- Per-leader multiplier: 1.0 for proven wallets, 0.2-0.5 while testing a
  new one.
- Keep "skip migrated curves" on unless you want entries into graduated
  coins; sells there route through PumpSwap and slip more than on the curve.
- Copy sells are proportional: leader trims 30%, you trim 30% of your bag. Leader dumps everything, you dump everythin.
- something looks wrong: Trading tab, verbose debug logs, then read `logs/pumpcopy-*.log`. Every rpc failure and tx signature ends up there.

## Donate
- SOL `Huw2ywYDAjhr1VjBVP4bspLw2Pmx1GFcv3ZsmZUkxYDg`
- ETH `0x6c4698288525676B43f60aFCf39b2044811B9B85`

