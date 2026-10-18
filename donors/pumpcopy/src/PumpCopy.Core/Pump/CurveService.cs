using Solgrid.Pumpfun.Accounts;
using Solgrid.Pumpfun.Client;

namespace PumpCopy.Core.Pump;

// thin adapter over the sdk client so the rest of core does not change shape
public class CurveService
{
    private readonly PumpClient _client;
    private readonly Dictionary<string, (DateTime At, BondingCurve? Curve)> _cache = new();

    public CurveService(PumpClient client)
    {
        _client = client;
    }

    public async Task<BondingCurve?> GetAsync(string mint)
    {
        lock (_cache)
        {
            if (_cache.TryGetValue(mint, out var hit) && (DateTime.Now - hit.At).TotalSeconds < 3)
                return hit.Curve;
        }

        var curve = (await _client.GetCoinStateAsync(mint))?.Curve;
        lock (_cache)
        {
            _cache[mint] = (DateTime.Now, curve);
            if (_cache.Count > 256)
                _cache.Clear();
        }
        return curve;
    }

    public Task<CoinState?> GetStateAsync(string mint)
        => _client.GetCoinStateAsync(mint);
}
