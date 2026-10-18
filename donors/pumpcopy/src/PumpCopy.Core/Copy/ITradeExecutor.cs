namespace PumpCopy.Core.Copy;

public record CopyRecord(
    DateTime Time,
    string Leader,
    bool IsBuy,
    string Mint,
    double LeaderSol,
    double OurSol,
    string Status);

public interface ITradeExecutor
{
    Task<(bool Ok, string? Error, string? Signature)> BuyAsync(string mint, ulong solLamports, int slippageBps, ulong priorityFee);
    Task<(bool Ok, string? Error, string? Signature)> SellAsync(string mint, ulong tokens, int slippageBps, ulong priorityFee, bool closeAta);

    Task<ulong> GetSolBalanceAsync();
    Task<ulong> GetTokenBalanceAsync(string mint);

    // fired when a sent tx later fails on chain, so positions can revert
    event Action<string, ulong, ulong, bool>? TradeReverted;
}
