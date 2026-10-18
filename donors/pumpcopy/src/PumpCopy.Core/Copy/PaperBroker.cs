using Solgrid.Pumpfun.Accounts;
using Solgrid.Pumpfun.Math;

namespace PumpCopy.Core.Copy;

public record PaperFill(bool Ok, string? Error, ulong SolLamports, ulong Tokens, decimal PriceSol)
{
    public static PaperFill Fail(string why) => new(false, why, 0, 0, 0m);
}

// simulated fills against the live curve, no wallet needed
public class PaperBroker
{
    private readonly Func<string, Task<BondingCurve?>> _curves;

    public PaperBroker(Func<string, Task<BondingCurve?>> curves)
    {
        _curves = curves;
    }

    public async Task<PaperFill> BuyAsync(string mint, ulong solIn)
    {
        var curve = await _curves(mint);
        if (curve == null)
            return PaperFill.Fail("curve not found");
        if (curve.Complete)
            return PaperFill.Fail("migrated");

        var tokens = BondingMath.QuoteBuyTokens(curve.VirtualQuoteReserves, curve.VirtualTokenReserves, solIn);
        if (tokens == 0)
            return PaperFill.Fail("quote zero");

        var price = BondingMath.SpotPrice(curve.VirtualQuoteReserves, curve.VirtualTokenReserves);
        return new PaperFill(true, null, solIn, tokens, price);
    }

    public async Task<PaperFill> SellAsync(string mint, ulong tokensIn)
    {
        var curve = await _curves(mint);
        if (curve == null)
            return PaperFill.Fail("curve not found");

        var sol = BondingMath.QuoteSellQuote(curve.VirtualQuoteReserves, curve.VirtualTokenReserves, tokensIn);
        if (sol == 0)
            return PaperFill.Fail("quote zero");

        var price = BondingMath.SpotPrice(curve.VirtualQuoteReserves, curve.VirtualTokenReserves);
        return new PaperFill(true, null, sol, tokensIn, price);
    }
}
