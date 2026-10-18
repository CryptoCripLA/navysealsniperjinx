using System.Net.WebSockets;
using System.Net;
using Solnet.Rpc;
using Solnet.Rpc.Core.Sockets;
using Solnet.Rpc.Messages;
using Solnet.Rpc.Models;
using Solnet.Rpc.Types;

namespace PumpCopy.Core.Solana;

// logsSubscribe(mentions) per leader wallet, with reconnect + resubscribe
public class WsFeed : IAsyncDisposable
{
    private readonly string _url;
    private readonly string? _proxy;
    private readonly Dictionary<string, SubscriptionState> _subs = new();
    private readonly HashSet<string> _wanted = new();
    private readonly object _gate = new();
    private IStreamingRpcClient? _ws;
    private bool _disposing;
    private int _backoff = 2;

    public bool IsConnected => _ws?.State == WebSocketState.Open;
    public string Url => _url;

    public event Action<string, ResponseValue<LogInfo>>? LogNotification;
    public event Action<bool>? ConnectionChanged;

    public WsFeed(string url, string? proxy = null)
    {
        _url = url;
        _proxy = proxy;
    }

    public async Task<bool> ConnectAsync()
    {
        try
        {
            ClientWebSocket? sock = null;
            if (!string.IsNullOrWhiteSpace(_proxy))
            {
                sock = new ClientWebSocket();
                sock.Options.Proxy = new WebProxy(_proxy);
                sock.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
            }

            _ws = sock == null
                ? ClientFactory.GetStreamingClient(_url)
                : ClientFactory.GetStreamingClient(_url, null!, sock);
            _ws.ConnectionStateChangedEvent += OnStateChanged;
            await _ws.ConnectAsync();
            _backoff = 2;
            Logging.Log.Info($"ws connected {_url}");
            ConnectionChanged?.Invoke(true);
            await SubscribeWantedAsync();
            return true;
        }
        catch (Exception ex)
        {
            Logging.Log.Err($"ws connect {_url} failed: {ex.Message}");
            ConnectionChanged?.Invoke(false);
            return false;
        }
    }

    public async Task SubscribeLeaderAsync(string address)
    {
        lock (_gate)
        {
            _wanted.Add(address);
            if (_subs.ContainsKey(address)) return;
        }

        if (_ws == null || !IsConnected)
            return;

        await SubscribeNowAsync(address);
    }

    private async Task SubscribeNowAsync(string address)
    {
        if (_ws == null)
            return;
        var sub = await _ws.SubscribeLogInfoAsync(address, (state, value) =>
        {
            LogNotification?.Invoke(address, value);
        }, Commitment.Confirmed);

        lock (_gate) _subs[address] = sub;
        Logging.Log.Info($"ws subscribed mentions={address[..Math.Min(8, address.Length)]}... state={sub.State}");
    }

    private async Task SubscribeWantedAsync()
    {
        List<string> snapshot;
        lock (_gate) snapshot = _wanted.ToList();
        foreach (var addr in snapshot)
        {
            lock (_gate) { if (_subs.ContainsKey(addr)) continue; }
            try { await SubscribeNowAsync(addr); }
            catch (Exception ex) { Logging.Log.Warn($"resubscribe {addr[..8]}: {ex.Message}"); }
        }
    }

    public async Task UnsubscribeAllAsync()
    {
        List<SubscriptionState> snapshot;
        lock (_gate)
        {
            snapshot = _subs.Values.ToList();
            _subs.Clear();
        }

        if (_ws == null) return;
        foreach (var s in snapshot)
        {
            try { await _ws.UnsubscribeAsync(s); }
            catch (Exception ex) { Logging.Log.Dbg($"unsubscribe: {ex.Message}"); }
        }
    }

    public IReadOnlyCollection<string> Subscribed => _subs.Keys.ToList();

    private void OnStateChanged(object? sender, WebSocketState state)
    {
        Logging.Log.Warn($"ws state -> {state}");
        ConnectionChanged?.Invoke(state == WebSocketState.Open);

        if (_disposing) return;
        if (state is WebSocketState.Closed or WebSocketState.Aborted)
            _ = ReconnectLoopAsync();
    }

    private async Task ReconnectLoopAsync()
    {
        List<string> leaders;
        lock (_gate)
        {
            leaders = _subs.Keys.ToList();
            _subs.Clear();
        }

        while (!_disposing)
        {
            Logging.Log.Info($"ws reconnect in {_backoff}s");
            await Task.Delay(TimeSpan.FromSeconds(_backoff));
            _backoff = Math.Min(_backoff * 2, 30);

            if (await ConnectAsync())
            {
                foreach (var l in leaders)
                    await SubscribeLeaderAsync(l);
                return;
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        _disposing = true;
        await UnsubscribeAllAsync();
        if (_ws != null)
        {
            try { await _ws.DisconnectAsync(); } catch { }
        }
        GC.SuppressFinalize(this);
    }
}

