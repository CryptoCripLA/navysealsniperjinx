using PumpCopy.Core.Config;
using PumpCopy.Core.Pump;
using PumpCopy.Core.Tracking;
using Solgrid.Pumpfun.Events;

namespace PumpCopy.Core.Copy;

// filters -> sizing -> paper or live fill -> positions
public class CopyEngine
{
    private readonly PositionStore _positions;
    private readonly PaperBroker _paper;
    private readonly CurveService _curves;
    private readonly Random _rnd = new();
    private readonly Dictionary<string, (ulong Tokens, ulong Sol)> _lastBuy = new();
    private readonly Dictionary<string, SemaphoreSlim> _mintLocks = new();
    private readonly Dictionary<string, (ulong Tokens, DateTime At)> _inFlightSells = new();
    private ITradeExecutor? _executor;

    public BotConfig Config { get; set; }

    // sell-all and manual actions set this so copying stays out of the way
    public bool Paused { get; set; }

    public ITradeExecutor? Executor
    {
        get => _executor;
        set
        {
            _executor = value;
            if (value != null)
                value.TradeReverted += OnTradeReverted;
        }
    }

    public event Action<CopyRecord>? Executed;

    public CopyEngine(BotConfig cfg, PositionStore positions, PaperBroker paper, CurveService curves, ITradeExecutor? executor)
    {
        Config = cfg;
        _positions = positions;
        _paper = paper;
        _curves = curves;
        Executor = executor;
    }

    private void OnTradeReverted(string mint, ulong tokens, ulong sol, bool wasBuy)
    {
        if (wasBuy)
        {
            if (_lastBuy.TryGetValue(mint, out var lb))
                _positions.RevertBuy(mint, lb.Tokens, lb.Sol);
        }
        else
        {
            // failed sell: free the in-flight clamp and get out of the bag now,
            // waiting for the next leader sell costs real money
            _inFlightSells.Remove(mint);
            _positions.RevertSell(mint);
            _ = RetrySellAsync(mint);
        }
    }

    private async Task RetrySellAsync(string mint)
    {
        try
        {
            if (Executor == null || Config.Paper || Paused)
                return;
            await Task.Delay(1500);
            var lk = LockFor(mint);
            await lk.WaitAsync();
            try
            {
                var actual = await Executor.GetTokenBalanceAsync(mint);
                if (actual == 0)
                    return;
                var slip = Math.Min(Config.SellSlippageBps * 2, 3000);
                var (ok, err, sig) = await Executor.SellAsync(mint, actual, slip, Config.PriorityFee, true);
                if (!ok)
                {
                    Logging.Log.Warn($"retry sell {mint[..8]}: {err}");
                    return;
                }
                var q = await _paper.SellAsync(mint, actual);
                _inFlightSells[mint] = (actual, DateTime.Now);
                _positions.ApplySell(mint, actual, q?.SolLamports ?? 0, 0);
                Logging.Log.Info($"retry sell {mint[..8]}: {actual / 1e6:0.######} tok -> {Sig(sig)}");
            }
            finally
            {
                lk.Release();
            }
        }
        catch (Exception ex)
        {
            Logging.Log.Dbg($"retry sell {mint[..8]}: {ex.Message}");
        }
    }

    public async Task HandleAsync(TradeEvent trade, LeaderConfig leader)
    {
        if (Paused)
            return;

        var cfg = Config;
        var alias = leader.AliasOrAddr();

        if (cfg.DelayMs > 0)
            await Task.Delay(_rnd.Next(cfg.DelayMs));

        if (trade.IsBuy)
            await HandleBuyAsync(trade, leader, alias, cfg);
        else
            await HandleSellAsync(trade, leader, alias, cfg);
    }

    private async Task HandleBuyAsync(TradeEvent trade, LeaderConfig leader, string alias, BotConfig cfg)
    {
        var mint = trade.Mint.Key;
        double leaderSol = trade.SolAmount / 1e9;

        if (trade.SolAmount < (ulong)(cfg.MinLeaderSol * 1e9m))
        {
            Emit(alias, trade, 0, $"skipped: leader size {leaderSol:0.####} < min {cfg.MinLeaderSol}");
            return;
        }

        var pos = _positions.Get(mint);
        if (pos == null && _positions.Open.Count >= cfg.MaxPositions)
        {
            Emit(alias, trade, 0, $"skipped: max positions {cfg.MaxPositions}");
            return;
        }

        if (cfg.SkipMigrated)
        {
            var curve = await _curves.GetAsync(mint);
            if (curve != null && curve.Complete)
            {
                Emit(alias, trade, 0, "skipped: migrated");
                return;
            }
        }

        if (!cfg.RepeatBuys && _positions.Get(mint) != null)
        {
            Emit(alias, trade, 0, "skipped: position already open (repeat buys off)");
            return;
        }

        var solIn = SizeCalculator.BuySolLamports(cfg, leader, trade.SolAmount);
        if (solIn == 0)
        {
            Emit(alias, trade, 0, "skipped: dust size");
            return;
        }

        if (Executor != null)
        {
            var bal = await Executor.GetSolBalanceAsync();
            if (bal < solIn + 5_000_000)
            {
                Emit(alias, trade, 0, $"skipped: wallet sol {bal / 1e9:0.####} < size + fees");
                return;
            }
        }

        if (cfg.Paper || Executor == null)
        {
            var fill = await _paper.BuyAsync(mint, solIn);
            if (!fill.Ok)
            {
                Emit(alias, trade, 0, $"paper fail: {fill.Error}");
                return;
            }
            _positions.ApplyBuy(mint, fill.Tokens, solIn, trade.TokenAmount);
            Emit(alias, trade, fill.SolLamports / 1e9, $"paper buy {fill.Tokens / 1e6:0.##} tok @ {fill.PriceSol:0.########}");
            return;
        }

        var (ok, err, sig) = await Executor.BuyAsync(mint, solIn, cfg.SlippageBps, cfg.PriorityFee);
        if (!ok)
        {
            Emit(alias, trade, 0, $"buy error: {err}");
            return;
        }
        // real balance tracking is optimistic here, confirmed amounts come later
        var q = await _paper.BuyAsync(mint, solIn);
        _lastBuy[mint] = (q?.Tokens ?? 0, solIn);
        _positions.ApplyBuy(mint, q?.Tokens ?? 0, solIn, trade.TokenAmount);
        Emit(alias, trade, solIn / 1e9, $"sent {Sig(sig)}");
    }

    private async Task HandleSellAsync(TradeEvent trade, LeaderConfig leader, string alias, BotConfig cfg)
    {
        var mint = trade.Mint.Key;
        if (!cfg.CopySells)
            return;

        var lk = LockFor(mint);
        await lk.WaitAsync();
        try
        {
            await HandleSellCoreAsync(trade, leader, alias, cfg, mint);
        }
        finally
        {
            lk.Release();
        }
    }

    private SemaphoreSlim LockFor(string mint)
    {
        lock (_mintLocks)
        {
            if (!_mintLocks.TryGetValue(mint, out var lk))
                _mintLocks[mint] = lk = new SemaphoreSlim(1, 1);
            return lk;
        }
    }

    private async Task HandleSellCoreAsync(TradeEvent trade, LeaderConfig leader, string alias, BotConfig cfg, string mint)
    {
        var pos = _positions.Get(mint);
        if (pos == null || pos.Tokens == 0)
        {
            Logging.Log.Dbg($"copy [{alias}] SELL {mint[..8]}... -> ignored: no open position in this mint");
            return;
        }

        var tokens = SizeCalculator.SellTokens(trade, pos);
        if (tokens == 0)
            return;

        if (cfg.Paper || Executor == null)
        {
            var fill = await _paper.SellAsync(mint, tokens);
            if (!fill.Ok)
            {
                Emit(alias, trade, 0, $"paper fail: {fill.Error}");
                return;
            }
            var pnl = _positions.ApplySell(mint, tokens, fill.SolLamports, trade.TokenAmount);
            Emit(alias, trade, fill.SolLamports / 1e9, $"paper sell pnl {pnl / 1e9:+0.####;-0.####}");
            return;
        }

        // positions are optimistic; the chain is the truth, minus sells
        // already in flight or two leader sells in a row double-dump us
        var actual = await Executor.GetTokenBalanceAsync(mint);
        if (_inFlightSells.TryGetValue(mint, out var infl) && (DateTime.Now - infl.At).TotalSeconds < 60)
            actual = actual > infl.Tokens ? actual - infl.Tokens : 0;
        if (actual == 0)
        {
            Emit(alias, trade, 0, "skipped: no free tokens on chain (sell in flight?)");
            return;
        }
        if (tokens > actual) tokens = actual;
        var fullExit = tokens >= actual;

        var (ok, err, sig) = await Executor.SellAsync(mint, tokens, cfg.SellSlippageBps, cfg.PriorityFee, fullExit);
        if (!ok)
        {
            Emit(alias, trade, 0, $"sell error: {err}");
            return;
        }
        _inFlightSells[mint] = (tokens, DateTime.Now);
        var q = await _paper.SellAsync(mint, tokens);
        _positions.ApplySell(mint, tokens, q?.SolLamports ?? 0, trade.TokenAmount);
        Emit(alias, trade, 0, $"sent {Sig(sig)}");
    }

    private void Emit(string alias, TradeEvent trade, double ourSol, string status)
    {
        var mint = trade.Mint.Key;
        Logging.Log.Info($"copy [{alias}] {(trade.IsBuy ? "BUY " : "SELL")} {mint[..8]}... -> {status}");
        Executed?.Invoke(new CopyRecord(DateTime.Now, alias, trade.IsBuy, mint, trade.SolAmount / 1e9, ourSol, status));
    }

    private static string Sig(string? s) => string.IsNullOrEmpty(s) ? "tx" : s[..Math.Min(12, s.Length)] + "...";
}
