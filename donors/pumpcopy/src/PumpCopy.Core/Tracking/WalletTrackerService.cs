using PumpCopy.Core.Config;
using PumpCopy.Core.Solana;
using Solgrid.Pumpfun.Client;
using Solgrid.Pumpfun.Events;
using Solnet.Rpc.Messages;
using Solnet.Rpc.Models;
using Solnet.Wallet;

namespace PumpCopy.Core.Tracking;

// subscribes ws logs per leader wallet, pulls the tx, decodes pump trades
public class WalletTrackerService
{
    private const int SeenCap = 4096;

    private readonly RpcPool _rpc;
    private readonly PumpClient? _pump;
    private readonly Dictionary<string, LeaderConfig> _leaders = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, string> _poolMints = new();
    private readonly HashSet<string> _seen = new();
    private readonly LinkedList<string> _seenOrder = new();
    private readonly CancellationTokenSource _cts = new();
    private WsFeed? _feed;

    public event Action<TradeEvent, LeaderConfig>? TradeDetected;
    public event Action<bool>? ConnectionChanged;

    public bool Running => _feed != null;

    public WalletTrackerService(RpcPool rpc, PumpClient? pump = null)
    {
        _rpc = rpc;
        _pump = pump;
    }

    public async Task StartAsync(IEnumerable<LeaderConfig> leaders, string rpcUrl, string? proxy)
    {
        foreach (var l in leaders.Where(l => l.Enabled && !string.IsNullOrWhiteSpace(l.Address)))
            _leaders[l.Address.Trim()] = l;

        _feed = new WsFeed(ToWsUrl(rpcUrl), proxy);
        _feed.LogNotification += OnLogNotification;
        _feed.ConnectionChanged += ok => ConnectionChanged?.Invoke(ok);

        // subscriptions silently no-op while the socket is down, so the
        // connect has to finish before we subscribe anyone
        await ConnectWithRetryAsync();

        foreach (var addr in _leaders.Keys.ToList())
            await _feed.SubscribeLeaderAsync(addr);

        Logging.Log.Info($"tracker started, {_leaders.Count} leader(s)");
    }

    private async Task ConnectWithRetryAsync()
    {
        if (_feed == null) return;
        while (!_cts.IsCancellationRequested)
        {
            if (await _feed.ConnectAsync())
                return;
            try { await Task.Delay(TimeSpan.FromSeconds(5), _cts.Token); }
            catch (OperationCanceledException) { return; }
        }
    }

    public async Task AddLeaderAsync(LeaderConfig cfg)
    {
        _leaders[cfg.Address.Trim()] = cfg;
        if (_feed != null && _feed.IsConnected)
            await _feed.SubscribeLeaderAsync(cfg.Address.Trim());
    }

    public async Task RemoveLeaderAsync(string address)
    {
        _leaders.Remove(address.Trim());
        // ws feed has no per-address unsubscribe, reconnect cleans it
        Logging.Log.Dbg($"leader removed: {address}");
        await Task.CompletedTask;
    }

    private void OnLogNotification(string leader, ResponseValue<LogInfo> value)
    {
        var sig = value?.Value?.Signature;
        if (string.IsNullOrEmpty(sig))
            return;

        Logging.Log.Dbg($"ws mention from {(_leaders.TryGetValue(leader, out var lc) ? lc.AliasOrAddr() : leader[..8])}: {sig[..Math.Min(12, sig.Length)]}");

        lock (_seen)
        {
            if (!_seen.Add(sig))
                return;
            _seenOrder.AddLast(sig);
            if (_seenOrder.Count > SeenCap)
            {
                _seen.Remove(_seenOrder.First!.Value);
                _seenOrder.RemoveFirst();
            }
        }

        // logsSubscribe already carries the logs: decode here and skip the
        // getTransaction round trip entirely, public rpcs rate-limit hard
        var inline = new List<TradeEvent>();
        foreach (var line in value?.Value?.Logs ?? Array.Empty<string>())
        {
            if (EventDecoder.DecodeLogLine(line) is TradeEvent t
                && string.Equals(t.User.Key, leader, StringComparison.OrdinalIgnoreCase))
                inline.Add(t);
        }

        if (inline.Count > 0)
        {
            _ = DispatchAsync(leader, inline);
            return;
        }

        _ = ProcessAsync(leader, sig);
    }

    private async Task DispatchAsync(string leader, List<TradeEvent> trades)
    {
        foreach (var trade in trades)
        {
            if (!_leaders.TryGetValue(leader, out var cfg))
                continue;
            Logging.Log.Info($"leader trade: {cfg.AliasOrAddr()} {(trade.IsBuy ? "BUY " : "SELL")} {(double)trade.SolAmount / 1e9:0.####} SOL mint={trade.Mint.Key[..8]}...");
            TradeDetected?.Invoke(trade, cfg);
        }
        await Task.CompletedTask;
    }

    private async Task ProcessAsync(string leader, string sig)
    {
        try
        {
            var res = await _rpc.GetTransactionAsync(sig);
            if (!res.WasSuccessful || res.Result == null)
            {
                Logging.Log.Warn($"tracker: no tx meta for {sig}");
                return;
            }

            var mine = new List<TradeEvent>();
            foreach (var line in res.Result.Meta?.LogMessages ?? Array.Empty<string>())
            {
                var ev = EventDecoder.DecodeLogLine(line);
                TradeEvent? t = ev switch
                {
                    TradeEvent te => te,
                    AmmSwapEvent se => await MapAmmAsync(se),
                    _ => null,
                };
                if (t != null && string.Equals(t.User.Key, leader, StringComparison.OrdinalIgnoreCase))
                    mine.Add(t);
            }

            if (mine.Count == 0)
            {
                Logging.Log.Dbg($"tx {sig[..Math.Min(12, sig.Length)]}: no pump trade event for this wallet");
                return;
            }

            foreach (var trade in mine)
            {
                if (!_leaders.TryGetValue(leader, out var cfg))
                    continue;

                Logging.Log.Info($"leader trade: {cfg.AliasOrAddr()} {(trade.IsBuy ? "BUY " : "SELL")} {(double)trade.SolAmount / 1e9:0.####} SOL mint={trade.Mint.Key[..8]}...");
                TradeDetected?.Invoke(trade, cfg);
            }
        }
        catch (Exception ex)
        {
            Logging.Log.Dbg($"tracker process {sig}: {ex.Message}");
        }
    }

    // pumpswap events carry the pool, not the mint; resolve once per pool
    private async Task<TradeEvent?> MapAmmAsync(AmmSwapEvent se)
    {
        if (_pump == null)
            return null;

        var poolKey = se.Pool.Key;
        if (!_poolMints.TryGetValue(poolKey, out var mint))
        {
            var pool = await _pump.GetPoolAsync(se.Pool);
            if (pool == null)
                return null;
            mint = pool.BaseMint.Key;
            _poolMints[poolKey] = mint;
        }

        var zero = new PublicKey(new byte[32]);
        return new TradeEvent(new PublicKey(mint), se.QuoteAmount, se.BaseAmount, se.IsBuy, se.User,
            se.Timestamp, 0, 0, 0, 0, zero, 0, 0, zero, 0, 0, false, 0, "poolswap", false, 0, 0, zero,
            se.QuoteAmount, 0, 0);
    }

    public async Task StopAsync()
    {
        _cts.Cancel();
        if (_feed != null)
        {
            await _feed.DisposeAsync();
            _feed = null;
        }
        Logging.Log.Info("tracker stopped");
    }

    public static string ToWsUrl(string httpUrl)
    {
        var u = httpUrl.Trim();
        if (u.StartsWith("https://")) return "wss://" + u["https://".Length..];
        if (u.StartsWith("http://")) return "ws://" + u["http://".Length..];
        return u;
    }
}

public static class LeaderConfigExt
{
    public static string AliasOrAddr(this LeaderConfig cfg)
        => string.IsNullOrWhiteSpace(cfg.Alias)
            ? (cfg.Address.Length > 8 ? cfg.Address[..8] : cfg.Address)
            : cfg.Alias;
}
