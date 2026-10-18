using PumpCopy.Core.Config;
using PumpCopy.Core.Tracking;
using Solgrid.Pumpfun.Accounts;
using Solgrid.Pumpfun.Events;
using Solgrid.Pumpfun.Math;
using Solnet.Wallet;
using Solnet.Wallet.Utilities;

namespace PumpCopy.Core.Copy;

// synthetic leader activity so the ui shows life without keys or funds.
// keeps its own virtual curves per mint, so paper fills stay consistent
// with what the "leaders" did.
public class DemoFeed
{
    private class SimMint
    {
        public ulong VSol = 30_000_000_000;
        public ulong VTok = 1_073_000_000_000_000;
        public ulong LeaderTokens;
    }

    private readonly Dictionary<string, SimMint> _mints = new();
    private readonly List<LeaderConfig> _leaders = new();
    private readonly Random _rnd = new();
    private CancellationTokenSource? _cts;

    public event Action<TradeEvent, LeaderConfig>? Trade;

    public DemoFeed()
    {
        CurveProvider = m => Task.FromResult(GetCurve(m));
        for (int i = 0; i < 3; i++)
            _mints[RandomKey()] = new SimMint();
    }

    public Func<string, Task<BondingCurve?>> CurveProvider { get; }

    public void Start(IEnumerable<LeaderConfig> leaders)
    {
        _leaders.AddRange(leaders.Where(l => l.Enabled));
        if (_leaders.Count == 0)
            _leaders.Add(new LeaderConfig { Address = RandomKey(), Alias = "demo-whale" });

        _cts = new CancellationTokenSource();
        _ = LoopAsync(_cts.Token);
        Logging.Log.Info($"demo feed on, {_leaders.Count} synthetic leader(s), {_mints.Count} mints");
    }

    public void Stop()
    {
        _cts?.Cancel();
        _cts = null;
    }

    private BondingCurve? GetCurve(string mint)
    {
        return _mints.TryGetValue(mint, out var s)
            ? new BondingCurve { VirtualQuoteReserves = s.VSol, VirtualTokenReserves = s.VTok }
            : null;
    }

    private async Task LoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(_rnd.Next(3000, 9000), ct);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            try { Step(); }
            catch (Exception ex) { Logging.Log.Dbg($"demo step: {ex.Message}"); }
        }
    }

    private void Step()
    {
        var leader = _leaders[_rnd.Next(_leaders.Count)];
        var mint = _mints.Keys.ElementAt(_rnd.Next(_mints.Count));
        var sim = _mints[mint];

        bool isBuy = sim.LeaderTokens == 0 || _rnd.Next(100) < 65;
        ulong sol;
        ulong tokens;

        if (isBuy)
        {
            sol = (ulong)((_rnd.NextDouble() * 3 + 0.6) * 1e9);
            tokens = BondingMath.QuoteBuyTokens(sim.VSol, sim.VTok, sol);
            if (tokens == 0) return;
            (sim.VSol, sim.VTok) = BondingMath.AfterBuy(sim.VSol, sim.VTok, sol, tokens);
            sim.LeaderTokens += tokens;
        }
        else
        {
            bool full = _rnd.Next(100) < 30;
            tokens = full ? sim.LeaderTokens : sim.LeaderTokens * (ulong)_rnd.Next(20, 60) / 100;
            if (tokens == 0) return;
            sol = BondingMath.QuoteSellQuote(sim.VSol, sim.VTok, tokens);
            (sim.VSol, sim.VTok) = BondingMath.AfterSell(sim.VSol, sim.VTok, sol, tokens);
            sim.LeaderTokens -= tokens;
        }

        var trade = new TradeEvent(
            Mint: new PublicKey(mint),
            SolAmount: sol,
            TokenAmount: tokens,
            IsBuy: isBuy,
            User: new PublicKey(leader.Address),
            Timestamp: DateTimeOffset.Now.ToUnixTimeSeconds(),
            VirtualSolReserves: sim.VSol,
            VirtualTokenReserves: sim.VTok,
            RealSolReserves: 0,
            RealTokenReserves: 0,
            FeeRecipient: new PublicKey(new byte[32]),
            FeeBasisPoints: 100,
            Fee: 0,
            Creator: new PublicKey(new byte[32]),
            CreatorFeeBasisPoints: 0,
            CreatorFee: 0,
            TrackVolume: false,
            CurrentSolVolume: 0,
            IxName: isBuy ? "buy" : "sell",
            MayhemMode: false,
            Cashback: 0,
            BuybackFee: 0,
            QuoteMint: new PublicKey(new byte[32]),
            QuoteAmount: sol,
            VirtualQuoteReserves: sim.VSol,
            RealQuoteReserves: 0);

        Trade?.Invoke(trade, leader);
    }

    private string RandomKey()
    {
        var bytes = new byte[32];
        _rnd.NextBytes(bytes);
        return Encoders.Base58.EncodeData(bytes);
    }
}
