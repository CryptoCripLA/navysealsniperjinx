using PumpCopy.Core.Config;
using PumpCopy.Core.Tracking;
using Solgrid.Pumpfun.Events;

namespace PumpCopy.Core.Copy;

public static class SizeCalculator
{
    public const ulong DustLamports = 1_000_000; // 0.001 SOL

    // 0 means "don't trade this one"
    public static ulong BuySolLamports(BotConfig cfg, LeaderConfig leader, ulong leaderSolLamports)
    {
        decimal sol = cfg.SizeMode == "mult"
            ? (leaderSolLamports / 1e9m) * cfg.Multiplier * leader.Mult
            : cfg.FixedSol * leader.Mult;

        if (sol > cfg.MaxOurSol) sol = cfg.MaxOurSol;

        var lamports = (ulong)Math.Round(sol * 1e9m);
        return lamports < DustLamports ? 0 : lamports;
    }

    // proportional to what the leader is exiting; full exit -> sell everything
    public static ulong SellTokens(TradeEvent trade, Position pos)
    {
        if (pos.Tokens == 0)
            return 0;

        ulong leaderHeld = pos.LeaderTokens;
        if (leaderHeld == 0)
            return pos.Tokens;

        if (trade.TokenAmount >= leaderHeld)
            return pos.Tokens;

        var tokens = (ulong)((decimal)pos.Tokens * trade.TokenAmount / leaderHeld);
        return Math.Min(tokens, pos.Tokens);
    }
}
