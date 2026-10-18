using System.Diagnostics;
using System.Net;
using Solnet.Rpc;
using Solnet.Rpc.Core.Http;
using Solnet.Rpc.Messages;
using Solnet.Rpc.Models;
using Solnet.Rpc.Types;

namespace PumpCopy.Core.Solana;

// two endpoints, active + backup. any failed call flips to the other one once.
public class RpcPool
{
    private readonly List<IRpcClient> _clients = new();
    private readonly List<string> _urls = new();
    private int _active;
    private int _rr;

    public string ActiveUrl => _urls.Count > 0 ? _urls[_active] : "";
    public IRpcClient ActiveClient => _clients[_active];
    public long LastLatencyMs { get; private set; } = -1;

    public RpcPool(IEnumerable<string> urls, string? proxy = null)
    {
        HttpClient? hc = null;
        if (!string.IsNullOrWhiteSpace(proxy))
        {
            hc = new HttpClient(new HttpClientHandler
            {
                Proxy = new WebProxy(proxy),
                UseProxy = true,
            });
        }

        foreach (var u in urls.Where(u => !string.IsNullOrWhiteSpace(u)))
        {
            _urls.Add(u.Trim());
            _clients.Add(hc == null
                ? ClientFactory.GetClient(u.Trim())
                : ClientFactory.GetClient(u.Trim(), null!, hc, null!));
        }

        if (_clients.Count == 0)
            throw new ArgumentException("no rpc urls configured");
    }

    public async Task<bool> ProbeAsync()
    {
        for (int i = 0; i < _clients.Count; i++)
        {
            var sw = Stopwatch.StartNew();
            try
            {
                var res = await _clients[i].GetSlotAsync(Commitment.Confirmed);
                sw.Stop();
                if (res.WasSuccessful)
                {
                    _active = i;
                    LastLatencyMs = sw.ElapsedMilliseconds;
                    return true;
                }
                Logging.Log.Warn($"rpc probe {i} ({_urls[i]}): {res.Reason}");
            }
            catch (Exception ex)
            {
                Logging.Log.Warn($"rpc probe {i} ({_urls[i]}): {ex.Message}");
            }
        }
        return false;
    }

    public async Task<RequestResult<T>> CallAsync<T>(Func<IRpcClient, Task<RequestResult<T>>> op, string what)
    {
        var sw = Stopwatch.StartNew();
        int n = _clients.Count;
        // round-robin start so rate limits spread across all endpoints,
        // then walk the ring until one answers
        int start = n == 1 ? 0 : (Interlocked.Increment(ref _rr) - 1) % n;
        RequestResult<T> last = default!;
        for (int attempt = 0; attempt < n; attempt++)
        {
            int idx = (start + attempt) % n;
            sw.Restart();
            try
            {
                var res = await op(_clients[idx]);
                if (res.WasSuccessful)
                {
                    _active = idx;
                    LastLatencyMs = sw.ElapsedMilliseconds;
                    return res;
                }
                last = res;
                Logging.Log.Dbg($"{what}: {_urls[idx]} answered {res.Reason}, trying next");
            }
            catch (Exception ex)
            {
                Logging.Log.Dbg($"{what}: {_urls[idx]} transport {ex.Message}, trying next");
                last = new RequestResult<T>(new HttpResponseMessage(HttpStatusCode.RequestTimeout), default!);
            }
        }

        Logging.Log.Warn($"{what}: all {n} rpc endpoints failed");
        return last;
    }

    private static RequestResult<T> Fail<T>(string why)
        => new(new HttpResponseMessage(HttpStatusCode.RequestTimeout) { ReasonPhrase = why }, default!);

    public Task<RequestResult<ResponseValue<AccountInfo>>> GetAccountInfoAsync(string address, Commitment c = Commitment.Confirmed)
        => CallAsync(cl => cl.GetAccountInfoAsync(address, c), $"getAccountInfo({address[..Math.Min(8, address.Length)]})");

    public Task<RequestResult<ResponseValue<LatestBlockHash>>> GetLatestBlockHashAsync()
        => CallAsync(cl => cl.GetLatestBlockHashAsync(Commitment.Confirmed), "getLatestBlockHash");

    public Task<RequestResult<TransactionMetaSlotInfo>> GetTransactionAsync(string signature)
        => CallAsync(cl => cl.GetTransactionAsync(signature, Commitment.Confirmed, 0), $"getTransaction({signature[..Math.Min(8, signature.Length)]})");

    public Task<RequestResult<string>> SendTransactionAsync(byte[] tx, bool skipPreflight = false)
    {
        Logging.Log.Dbg($"sendTransaction via {ActiveUrl} len={tx.Length} skipPreflight={skipPreflight}");
        return CallAsync(cl => cl.SendTransactionAsync(tx, skipPreflight, Commitment.Confirmed), "sendTransaction");
    }
}

