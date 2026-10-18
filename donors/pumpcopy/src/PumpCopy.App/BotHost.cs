using System.IO;
using PumpCopy.Core.Config;
using PumpCopy.Core.Logging;
using PumpCopy.Core.Copy;
using PumpCopy.Core.Pump;
using PumpCopy.Core.Solana;
using PumpCopy.Core.Tracking;
using Solgrid.Pumpfun.Client;
using Solgrid.Pumpfun.Accounts;
using Solgrid.Pumpfun.Events;
using Solnet.Wallet;

namespace PumpCopy.App;

// glue between core services and the ui thread
public class BotHost
{
    private readonly ConfigStore _store;
    private WalletTrackerService? _tracker;
    private CopyEngine? _engine;
    private DemoFeed? _demo;
    private PumpClient? _pumpClient;

    public BotConfig Config { get; private set; }
    public PositionStore Positions { get; } = new();
    public RpcPool? Pool { get; private set; }
    public bool Running { get; private set; }

    public event Action<CopyRecord>? Record;
    public event Action? StatsChanged;
    public event Action<bool>? ConnectionChanged;

    public BotHost(ConfigStore store, BotConfig config)
    {
        _store = store;
        Config = config;
    }

    public async Task<string?> StartAsync()
    {
        if (Running)
            return null;

        Log.Init(min: Config.VerboseLogs ? LogLevel.Dbg : ParseLevel(Config.LogLevel));
        Log.FileEnabled = Config.Logging;
        Solgrid.Pumpfun.SdkLog.OnTrace = m => Log.Dbg("[sdk] " + m);
        Log.Info($"starting, mode={(Config.Paper ? "paper" : "live")}, leaders={Config.Leaders.Count(l => l.Enabled)}, verbose={Config.VerboseLogs}");

        Pool = new RpcPool(Config.Rpcs, ProxyUrl());
        if (!await Pool.ProbeAsync())
            return "all rpc endpoints unreachable";

        Account? trader = null;
        if (!Config.Paper)
        {
            trader = LoadTrader();
            if (trader == null)
                return "no trading key loaded (check wallet tab / key file)";
            Log.Info($"trader: {trader.PublicKey.Key[..8]}...");
        }

        var pumpClient = new PumpClient(Pool.ActiveClient, trader);
        _pumpClient = pumpClient;

        var curves = new CurveService(pumpClient);
        Func<string, Task<BondingCurve?>> curveSource = curves.GetAsync;
        if (Config.DemoFeed && Config.Paper)
        {
            _demo = new DemoFeed();
            _demo.Trade += (trade, leader) => _ = SafeHandleAsync(trade, leader);
            curveSource = _demo.CurveProvider;
        }

        var paper = new PaperBroker(curveSource);
        ITradeExecutor? executor = trader == null ? null : new PumpExecutor(pumpClient);

        _engine = new CopyEngine(Config, Positions, paper, curves, executor);
        _engine.Executed += r => Record?.Invoke(r);

        if (!Config.Paper)
            await ReconstructPositionsAsync(pumpClient);

        _tracker = new WalletTrackerService(Pool, pumpClient);
        _tracker.TradeDetected += (trade, leader) => _ = SafeHandleAsync(trade, leader);
        _tracker.ConnectionChanged += ok => ConnectionChanged?.Invoke(ok);
        await _tracker.StartAsync(Config.Leaders, Config.Rpcs[0], ProxyUrl());
        _demo?.Start(Config.Leaders);

        Positions.Changed += () => StatsChanged?.Invoke();
        Running = true;
        return null;
    }

    public async Task StopAsync()
    {
        if (!Running)
            return;
        Running = false;
        _demo?.Stop();
        _demo = null;
        if (_tracker != null)
            await _tracker.StopAsync();
        _tracker = null;
        _engine = null;
        Log.Info("bot stopped");
    }

    public void ApplyConfig(BotConfig cfg)
    {
        Config = cfg;
        if (_engine != null)
            _engine.Config = cfg;
    }

    public void SaveConfig()
    {
        _store.Save(Config);
    }

    private async Task SafeHandleAsync(TradeEvent trade, LeaderConfig leader)
    {
        try
        {
            if (_engine != null)
                await _engine.HandleAsync(trade, leader);
        }
        catch (Exception ex)
        {
            Log.Err($"copy pipeline: {ex.Message}");
        }
    }

    private Account? LoadTrader()
    {
        try
        {
            var wallets = WalletStore.Load(WalletStore.ResolvePath(Config.KeyFile));
            if (wallets.Count == 0)
                return null;
            var idx = Math.Min(Config.TradingWallet, wallets.Count - 1);
            return wallets[idx];
        }
        catch (Exception ex)
        {
            Log.Err($"key load: {ex.Message}");
            return null;
        }
    }

    // positions live in memory; after a restart the chain still holds bags
    // that copy-sells would otherwise ignore forever
    private async Task ReconstructPositionsAsync(PumpClient client)
    {
        if (client.Trader == null || Pool == null)
            return;

        foreach (var prog in new[] { "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" })
        {
            var res = await Pool.CallAsync(cl => cl.GetTokenAccountsByOwnerAsync(client.Trader.PublicKey.Key, null, prog), "getTokenAccountsByOwner");
            foreach (var acc in res.Result?.Value ?? new List<Solnet.Rpc.Models.TokenAccount>())
            {
                var info = acc.Account?.Data?.Parsed?.Info;
                if (info == null || !ulong.TryParse(info.TokenAmount?.Amount, out var amount) || amount == 0)
                    continue;
                var mint = info.Mint;
                if (mint == "So111111111111111111111111111112")
                    continue;
                if (Positions.Get(mint) != null)
                    continue;

                var state = await client.GetCoinStateAsync(mint);
                if (state?.Curve == null)
                    continue;

                Positions.ApplyBuy(mint, amount, 0, amount);
                Log.Info($"positions: reconstructed {mint[..8]}... {amount / 1e6:0.######} tok from chain (cost basis unknown)");
            }
        }
    }

    // dumps every pump coin the trading wallet still holds, migrated ones
    // through the pool, curve ones with ata close on full exit
    public async Task SellAllAsync()
    {
        var wasRunning = Running;
        if (wasRunning && _engine != null)
        {
            _engine.Paused = true;
            Log.Info("sell all: copying paused while selling");
        }

        RpcPool? tmpPool = null;
        try
        {
            var exec = _engine?.Executor as PumpExecutor;
            var client = exec?.Client;
            if (exec == null || client == null)
            {
                tmpPool = new RpcPool(Config.Rpcs, ProxyUrl());
                var trader = LoadTrader();
                if (trader == null)
                {
                    Log.Err("sell all: no trading key loaded");
                    return;
                }
                client = new PumpClient(tmpPool.ActiveClient, trader);
                exec = new PumpExecutor(client);
            }

            var traderKey = client.Trader;
            if (traderKey == null)
                return;

        var programs = new[] { "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" };
        foreach (var prog in programs)
        {
            var res = await tmpPoolOrActive(tmpPool).CallAsync(cl => cl.GetTokenAccountsByOwnerAsync(traderKey.PublicKey.Key, null, prog), "getTokenAccountsByOwner");
            var accounts = res.Result?.Value ?? new List<Solnet.Rpc.Models.TokenAccount>();
            Log.Dbg($"sell all: {prog[..8]}... returned {accounts.Count} token account(s)");
            foreach (var acc in accounts)
            {
                var info = acc.Account?.Data?.Parsed?.Info;
                if (info == null)
                    continue;
                if (!ulong.TryParse(info.TokenAmount?.Amount, out var amount) || amount == 0)
                    continue;
                var mint = info.Mint;
                Log.Dbg($"sell all: mint={mint} amount={amount}");
                if (mint == "So111111111111111111111111111112")
                    continue;

                var state = await client.GetCoinStateAsync(mint);
                if (state?.Curve == null)
                {
                    Log.Dbg($"sell all: {mint[..8]}... is not a pump coin, skipped");
                    continue;
                }

                var (ok, err, sig) = await exec.SellAsync(mint, amount, Config.SellSlippageBps, Config.PriorityFee, !state.Migrated);
                Log.Info(ok
                    ? $"sell all: {mint[..8]}... {amount / 1e6:0.######} tok -> {Sig(sig)}"
                    : $"sell all: {mint[..8]}... failed: {err}");
                await Task.Delay(500);
            }
        }

        Log.Info("sell all: done");
        }
        finally
        {
            if (wasRunning && _engine != null)
            {
                _engine.Paused = false;
                Log.Info("sell all: copying resumed");
            }
        }
    }

    private RpcPool tmpPoolOrActive(RpcPool? tmp) => tmp ?? Pool!;

    private ITradeExecutor? _executor() => _engine?.Executor;

    private static string Sig(string? s) => string.IsNullOrEmpty(s) ? "tx" : s[..Math.Min(12, s.Length)] + "...";

    public string? ProxyUrl()
        => Config.UseProxy && Config.Proxies.Count > 0
            ? Config.Proxies[Math.Min(Config.ActiveProxy, Config.Proxies.Count - 1)]
            : null;

    // short-lived pool for ui actions while the bot is stopped
    public async Task<Dictionary<string, double>> GetBalancesAsync(IEnumerable<string> pubkeys)
    {
        var result = new Dictionary<string, double>();
        var pool = Pool ?? new RpcPool(Config.Rpcs, ProxyUrl());
        foreach (var key in pubkeys)
        {
            var res = await pool.CallAsync(cl => cl.GetBalanceAsync(key), "getBalance");
            result[key] = res.WasSuccessful && res.Result?.Value != null ? res.Result.Value / 1e9 : double.NaN;
        }
        return result;
    }

    private static LogLevel ParseLevel(string s) => s.ToLowerInvariant() switch
    {
        "dbg" or "debug" => LogLevel.Dbg,
        "warn" => LogLevel.Warn,
        "err" or "error" => LogLevel.Err,
        _ => LogLevel.Info,
    };
}


