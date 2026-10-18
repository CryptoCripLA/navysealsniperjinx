using Solnet.Programs;
using Solgrid.Pumpfun.Client;

namespace PumpCopy.Core.Copy;

// live execution delegated to the sdk client: it picks v1/v2 by quote mint
// and routes migrated coins through pumpswap
public class PumpExecutor : ITradeExecutor
{
    private readonly PumpClient _client;

    public PumpClient Client => _client;

    public event Action<string, ulong, ulong, bool>? TradeReverted;

    public PumpExecutor(PumpClient client)
    {
        _client = client;
    }

    public async Task<(bool Ok, string? Error, string? Signature)> BuyAsync(string mint, ulong solLamports, int slippageBps, ulong priorityFee)
    {
        var res = await _client.BuyAsync(mint, solLamports, slippageBps, priorityFee);
        if (res.Ok && res.Signature != null)
        {
            var sig = res.Signature;
            _ = ConfirmAsync(sig, $"buy {mint[..6]}", () => TradeReverted?.Invoke(mint, 0, solLamports, true));
        }
        return res;
    }

    public async Task<(bool Ok, string? Error, string? Signature)> SellAsync(string mint, ulong tokens, int slippageBps, ulong priorityFee, bool closeAta)
    {
        var res = await _client.SellAsync(mint, tokens, slippageBps, priorityFee, closeAta);
        if (res.Ok && res.Signature != null)
        {
            var sig = res.Signature;
            _ = ConfirmAsync(sig, $"sell {mint[..6]}", () => TradeReverted?.Invoke(mint, tokens, 0, false));
        }
        return res;
    }

    public async Task<ulong> GetSolBalanceAsync()
    {
        if (_client.Trader == null)
            return 0;
        var res = await _client.Rpc.GetBalanceAsync(_client.Trader.PublicKey.Key);
        return res.WasSuccessful && res.Result?.Value != null ? res.Result.Value : 0;
    }

    public async Task<ulong> GetTokenBalanceAsync(string mint)
    {
        if (_client.Trader == null)
            return 0;
        var ata = Solgrid.Pumpfun.Pda.Ata(_client.Trader.PublicKey, TokenProgram.ProgramIdKey, new Solnet.Wallet.PublicKey(mint));
        var ata2022 = Solgrid.Pumpfun.Pda.Ata(_client.Trader.PublicKey, new Solnet.Wallet.PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"), new Solnet.Wallet.PublicKey(mint));
        foreach (var addr in new[] { ata.Key, ata2022.Key })
        {
            var res = await _client.Rpc.GetTokenAccountBalanceAsync(addr);
            if (res.WasSuccessful && res.Result?.Value != null && ulong.TryParse(res.Result.Value.Amount, out var v))
                return v;
        }
        return 0;
    }

    // skipPreflight means failures only show up on chain; poll a bit so the
    // operator sees them in the log and positions stay truthful
    private async Task ConfirmAsync(string signature, string what, Action revert)
    {
        for (int i = 0; i < 5; i++)
        {
            await Task.Delay(3000);
            try
            {
                var st = await _client.Rpc.GetSignatureStatusesAsync(new List<string> { signature });
                var info = st.Result?.Value?.FirstOrDefault();
                if (info == null)
                {
                    Logging.Log.Dbg($"{what}: poll {i}: not seen yet");
                    continue;
                }
                Logging.Log.Dbg($"{what}: poll {i}: status={info.ConfirmationStatus} err={(info.Error == null ? "null" : "yes")}");
                if (info.Error != null)
                {
                    Logging.Log.Warn($"{what}: tx {signature[..12]}... FAILED on chain: {System.Text.Json.JsonSerializer.Serialize(info.Error)}");
                    revert();
                    return;
                }
                if (info.ConfirmationStatus is "confirmed" or "finalized")
                {
                    Logging.Log.Info($"{what}: tx {signature[..12]}... confirmed");
                    return;
                }
            }
            catch
            {
                return;
            }
        }
        Logging.Log.Warn($"{what}: tx {signature[..12]}... not confirmed in 20s — likely dropped by the rpc, check rpc order in Connection tab");
    }
}
