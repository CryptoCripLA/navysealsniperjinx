using PumpCopy.Core.Config;
using PumpCopy.Core.Copy;
using PumpCopy.Core.Pump;
using PumpCopy.Core.Tracking;
using Solgrid.Pumpfun.Accounts;
using Solgrid.Pumpfun.Events;
using Solnet.Wallet;

namespace PumpCopy.Core.Tests;

public class CopySizingTests
{
    private static LeaderConfig Leader(decimal mult = 1.0m) => new() { Address = "x", Mult = mult };

    private static TradeEvent SellEvent(ulong tokens) => new(
        Mint: new PublicKey(new byte[32]),
        SolAmount: 1,
        TokenAmount: tokens,
        IsBuy: false,
        User: new PublicKey(new byte[32]),
        Timestamp: 0,
        VirtualSolReserves: 0,
        VirtualTokenReserves: 0,
        RealSolReserves: 0,
        RealTokenReserves: 0,
        FeeRecipient: new PublicKey(new byte[32]),
        FeeBasisPoints: 0,
        Fee: 0,
        Creator: new PublicKey(new byte[32]),
        CreatorFeeBasisPoints: 0,
        CreatorFee: 0,
        TrackVolume: false,
        CurrentSolVolume: 0,
        IxName: "sell",
        MayhemMode: false,
        Cashback: 0,
        BuybackFee: 0,
        QuoteMint: new PublicKey(new byte[32]),
        QuoteAmount: 0,
        VirtualQuoteReserves: 0,
        RealQuoteReserves: 0);

    [Fact]
    public void FixedMode_UsesFixedSol_CappedByMax()
    {
        var cfg = new BotConfig { SizeMode = "fixed", FixedSol = 0.5m, MaxOurSol = 0.3m };
        var sol = SizeCalculator.BuySolLamports(cfg, Leader(), 5_000_000_000);
        Assert.Equal(300_000_000UL, sol);

        cfg.MaxOurSol = 1.0m;
        sol = SizeCalculator.BuySolLamports(cfg, Leader(), 5_000_000_000);
        Assert.Equal(500_000_000UL, sol);
    }

    [Fact]
    public void MultMode_FollowsLeader_AndLeaderMult()
    {
        var cfg = new BotConfig { SizeMode = "mult", Multiplier = 0.5m, MaxOurSol = 10m };
        var sol = SizeCalculator.BuySolLamports(cfg, Leader(2.0m), 4_000_000_000);
        Assert.Equal(4_000_000_000UL, sol); // 4 * 0.5 * 2
    }

    [Fact]
    public void Dust_ReturnsZero()
    {
        var cfg = new BotConfig { SizeMode = "fixed", FixedSol = 0.0005m, MaxOurSol = 1m };
        Assert.Equal(0UL, SizeCalculator.BuySolLamports(cfg, Leader(), 1_000_000_000));
    }

    [Fact]
    public void SellProportional_HalfExit()
    {
        var pos = new Position { Mint = "m", Tokens = 1_000_000, LeaderTokens = 800_000 };
        Assert.Equal(500_000UL, SizeCalculator.SellTokens(SellEvent(400_000), pos));
    }

    [Fact]
    public void SellFullExit_SellsEverything()
    {
        var pos = new Position { Mint = "m", Tokens = 1_000_000, LeaderTokens = 800_000 };
        Assert.Equal(1_000_000UL, SizeCalculator.SellTokens(SellEvent(900_000), pos));
    }

    [Fact]
    public void PositionStore_PnlOnFullRoundTrip()
    {
        var store = new PositionStore();
        store.ApplyBuy("m", 1_000_000, 100_000_000, 500_000);
        var pnl = store.ApplySell("m", 1_000_000, 120_000_000, 500_000);

        Assert.Equal(20_000_000, pnl);
        Assert.Equal(20_000_000, store.SessionPnlLamports);
        Assert.Empty(store.Open);
        Assert.Single(store.Closed);
    }

    [Fact]
    public async Task PaperBroker_BuySell_UsesCurve()
    {
        var curve = new BondingCurve
        {
            VirtualQuoteReserves = 30_000_000_000,
            VirtualTokenReserves = 1_073_000_000_000_000,
        };
        var broker = new PaperBroker(_ => Task.FromResult<BondingCurve?>(curve));

        var buy = await broker.BuyAsync("m", 1_000_000_000);
        Assert.True(buy.Ok);
        Assert.True(buy.Tokens > 0);

        var sell = await broker.SellAsync("m", buy.Tokens);
        Assert.True(sell.Ok);
        Assert.True(sell.SolLamports < 1_000_000_000);
    }
}
