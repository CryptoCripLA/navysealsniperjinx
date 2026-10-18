using System.Collections.ObjectModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using PumpCopy.App.ViewModels;
using PumpCopy.Core.Solana;

namespace PumpCopy.App.Views;

public class RpcRowVm : ViewModelBase
{
    private bool _isPrimary;
    private string _status = "-";
    private string _ms = "-";

    public string Url { get; init; } = "";

    public bool IsPrimary
    {
        get => _isPrimary;
        set
        {
            if (Set(ref _isPrimary, value) && value)
                PrimaryPicked?.Invoke(this);
        }
    }

    public string Status { get => _status; set => Set(ref _status, value); }
    public string Ms { get => _ms; set => Set(ref _ms, value); }
    public event Action<RpcRowVm>? PrimaryPicked;
}

public class ProxyRowVm : ViewModelBase
{
    private bool _isActive;
    private string _status = "-";

    public string Url { get; init; } = "";

    public bool IsActive
    {
        get => _isActive;
        set
        {
            if (Set(ref _isActive, value) && value)
                ActivePicked?.Invoke(this);
        }
    }

    public string Status { get => _status; set => Set(ref _status, value); }
    public event Action<ProxyRowVm>? ActivePicked;
}

public partial class ConnectionView : UserControl
{
    private readonly BotHost _host;
    private readonly ObservableCollection<RpcRowVm> _rpcs = new();
    private readonly ObservableCollection<ProxyRowVm> _proxies = new();

    public ConnectionView(BotHost host)
    {
        InitializeComponent();
        _host = host;
        RpcGrid.ItemsSource = _rpcs;
        ProxyGrid.ItemsSource = _proxies;

        int i = 0;
        foreach (var url in host.Config.Rpcs)
            AddRpcRow(url, i++ == 0);
        int p = 0;
        foreach (var px in host.Config.Proxies)
            AddProxyRow(px, p++ == host.Config.ActiveProxy);
        UseProxyCheck.IsChecked = host.Config.UseProxy;
    }

    private void AddRpcRow(string url, bool primary)
    {
        var row = new RpcRowVm { Url = url, IsPrimary = primary };
        row.PrimaryPicked += r =>
        {
            // primary means first in failover order
            _rpcs.Remove(r);
            _rpcs.Insert(0, r);
            foreach (var o in _rpcs) o.IsPrimary = o == r;
        };
        _rpcs.Add(row);
    }

    private void AddProxyRow(string url, bool active)
    {
        var row = new ProxyRowVm { Url = url, IsActive = active };
        row.ActivePicked += r =>
        {
            foreach (var o in _proxies) o.IsActive = o == r;
        };
        _proxies.Add(row);
    }

    private async void OnAddRpcClick(object sender, RoutedEventArgs e)
    {
        var dlg = new PromptDialog("add rpc", "rpc url (https://...)") { Owner = Window.GetWindow(this) };
        if (dlg.ShowDialog() != true)
            return;
        var url = dlg.Input.Trim();
        if (url.Length == 0 || _rpcs.Any(r => r.Url == url))
            return;

        var row = new RpcRowVm { Url = url, IsPrimary = _rpcs.Count == 0 };
        row.PrimaryPicked += r =>
        {
            _rpcs.Remove(r);
            _rpcs.Insert(0, r);
            foreach (var o in _rpcs) o.IsPrimary = o == r;
        };
        _rpcs.Add(row);
        await CheckRpcAsync(row);
    }

    private void OnRemoveRpcClick(object sender, RoutedEventArgs e)
    {
        if (RpcGrid.SelectedItem is RpcRowVm row)
        {
            _rpcs.Remove(row);
            if (_rpcs.Count > 0) _rpcs[0].IsPrimary = true;
        }
    }

    private async void OnCheckRpcClick(object sender, RoutedEventArgs e)
    {
        foreach (var row in _rpcs.ToList())
            await CheckRpcAsync(row);
    }

    private async Task CheckRpcAsync(RpcRowVm row)
    {
        row.Status = "checking...";
        var (ok, ms, err) = await RpcProbe.CheckAsync(row.Url, _host.ProxyUrl());
        row.Status = ok ? "ok" : $"fail: {Trunc(err)}";
        row.Ms = ok ? ms.ToString() : "-";
    }

    private void OnImportRpcClick(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFileDialog { Filter = "text files|*.txt|all files|*.*", Title = "import rpc list" };
        if (dlg.ShowDialog() != true)
            return;
        foreach (var line in File.ReadAllLines(dlg.FileName))
        {
            var url = line.Trim();
            if (url.Length == 0 || url.StartsWith("#") || _rpcs.Any(r => r.Url == url))
                continue;
            AddRpcRow(url, _rpcs.Count == 0);
        }
    }

    private void OnAddProxyClick(object sender, RoutedEventArgs e)
    {
        var dlg = new PromptDialog("add proxy", "socks5://host:port or socks5://user:pass@host:port") { Owner = Window.GetWindow(this) };
        if (dlg.ShowDialog() != true)
            return;
        var px = dlg.Input.Trim();
        if (px.Length == 0 || _proxies.Any(r => r.Url == px))
            return;
        if (!px.StartsWith("socks5://"))
        {
            MessageBox.Show("socks5 only: socks5://host:port or socks5://user:pass@host:port", "PumpCopy", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        AddProxyRow(px, _proxies.Count == 0);
    }

    private void OnRemoveProxyClick(object sender, RoutedEventArgs e)
    {
        if (ProxyGrid.SelectedItem is ProxyRowVm row)
        {
            _proxies.Remove(row);
            if (_proxies.Count > 0) _proxies[0].IsActive = true;
        }
    }

    private async void OnCheckProxyClick(object sender, RoutedEventArgs e)
    {
        if (ProxyGrid.SelectedItem is not ProxyRowVm row)
            return;
        row.Status = "checking...";
        var rpc = _rpcs.Count > 0 ? _rpcs[0].Url : "https://api.mainnet-beta.solana.com";
        var (ok, _, err) = await RpcProbe.CheckAsync(rpc, row.Url);
        row.Status = ok ? "ok" : $"fail: {Trunc(err)}";
    }

    private void OnImportProxyClick(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFileDialog { Filter = "text files|*.txt|all files|*.*", Title = "import proxy list (socks5)" };
        if (dlg.ShowDialog() != true)
            return;
        foreach (var line in File.ReadAllLines(dlg.FileName))
        {
            var px = line.Trim();
            if (px.Length == 0 || px.StartsWith("#") || _proxies.Any(r => r.Url == px))
                continue;
            if (!px.StartsWith("socks5://"))
                continue;
            AddProxyRow(px, _proxies.Count == 0);
        }
    }

    private void OnSaveClick(object sender, RoutedEventArgs e)
    {
        var c = _host.Config;
        c.Rpcs = _rpcs.Select(r => r.Url).ToList();
        if (c.Rpcs.Count == 0)
        {
            MessageBox.Show("need at least one rpc", "PumpCopy", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        c.Proxies = _proxies.Select(p => p.Url).ToList();
        c.ActiveProxy = Math.Max(0, _proxies.ToList().FindIndex(p => p.IsActive));
        c.UseProxy = UseProxyCheck.IsChecked == true;

        _host.ApplyConfig(c);
        _host.SaveConfig();
        SavedHint.Text = $"saved {DateTime.Now:HH:mm:ss}";
    }

    private static string Trunc(string? s) => s == null ? "?" : s.Length > 40 ? s[..40] + "..." : s;
}
