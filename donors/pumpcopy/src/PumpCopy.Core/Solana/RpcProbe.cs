using System.Diagnostics;
using System.Net;
using Solnet.Rpc;
using Solnet.Rpc.Types;

namespace PumpCopy.Core.Solana;

public static class RpcProbe
{
    public static async Task<(bool Ok, long Ms, string? Err)> CheckAsync(string url, string? proxy = null)
    {
        var sw = Stopwatch.StartNew();
        try
        {
            HttpClient? hc = null;
            if (!string.IsNullOrWhiteSpace(proxy))
            {
                hc = new HttpClient(new HttpClientHandler { Proxy = new WebProxy(proxy), UseProxy = true });
            }

            var client = hc == null ? ClientFactory.GetClient(url) : ClientFactory.GetClient(url, null!, hc, null!);
            var res = await client.GetSlotAsync(Commitment.Confirmed);
            sw.Stop();
            return res.WasSuccessful
                ? (true, sw.ElapsedMilliseconds, null)
                : (false, sw.ElapsedMilliseconds, res.Reason);
        }
        catch (Exception ex)
        {
            sw.Stop();
            return (false, sw.ElapsedMilliseconds, ex.Message);
        }
    }
}
