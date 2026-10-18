using System.Collections.ObjectModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using PumpCopy.App.ViewModels;
using PumpCopy.Core.Config;
using Solnet.Wallet;

namespace PumpCopy.App.Views;

public class WalletRowVm : ViewModelBase
{
    private bool _isTrading;
    private string _balance = "-";
    private bool _showFull;

    public string Secret { get; init; } = "";
    public string PubKey { get; init; } = "";

    public bool IsTrading
    {
        get => _isTrading;
        set
        {
            if (Set(ref _isTrading, value) && value)
                TradingPicked?.Invoke(this);
        }
    }

    public string Balance
    {
        get => _balance;
        set => Set(ref _balance, value);
    }

    public bool ShowFull
    {
        get => _showFull;
        set { Set(ref _showFull, value); OnPropertyChanged(nameof(KeyDisplay)); }
    }

    public string KeyDisplay => string.IsNullOrEmpty(Secret) ? "-" : ShowFull ? Secret : WalletStore.Mask(Secret);

    public event Action<WalletRowVm>? TradingPicked;
}

public partial class WalletView : UserControl
{
    private readonly BotHost _host;
    private readonly ObservableCollection<WalletRowVm> _rows = new();

    public WalletView(BotHost host)
    {
        InitializeComponent();
        _host = host;
        WalletGrid.ItemsSource = _rows;
        ShowKeysCheck.IsChecked = !host.Config.MaskKeys;
        LoadRows();
    }

    private void LoadRows()
    {
        _rows.Clear();
        var path = WalletStore.ResolvePath(_host.Config.KeyFile);
        if (!File.Exists(path))
            return;

        int idx = 0;
        foreach (var line in File.ReadAllLines(path))
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0 || trimmed.StartsWith("#") || trimmed.StartsWith("["))
                continue;
            if (!AddRow(trimmed, _host.Config.TradingWallet == idx))
                continue;
            idx++;
        }
    }

    private bool AddRow(string secret, bool trading)
    {
        try
        {
            var acc = Account.FromSecretKey(secret);
            var row = new WalletRowVm { Secret = secret, PubKey = acc.PublicKey.Key, ShowFull = ShowKeysCheck.IsChecked == true, IsTrading = trading };
            row.TradingPicked += r =>
            {
                foreach (var other in _rows.Where(o => o != r))
                    other.IsTrading = false;
            };
            _rows.Add(row);
            return true;
        }
        catch
        {
            return false;
        }
    }

    private void OnShowKeysChanged(object sender, RoutedEventArgs e)
    {
        if (ShowKeysCheck == null)
            return;
        foreach (var r in _rows)
            r.ShowFull = ShowKeysCheck.IsChecked == true;
    }

    private void OnImportClick(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFileDialog { Filter = "text files|*.txt|all files|*.*", Title = "import private keys" };
        if (dlg.ShowDialog() != true)
            return;

        int added = 0;
        foreach (var line in File.ReadAllLines(dlg.FileName))
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0 || trimmed.StartsWith("#") || trimmed.StartsWith("["))
                continue;
            if (_rows.Any(r => r.Secret == trimmed))
                continue;
            if (AddRow(trimmed, _rows.Count == 0))
                added++;
        }

        SavedHint.Text = $"imported {added} key(s)";
    }

    private void OnAddClick(object sender, RoutedEventArgs e)
    {
        var dlg = new PromptDialog("add wallet", "paste base58 private key (64 bytes secret)") { Owner = Window.GetWindow(this) };
        if (dlg.ShowDialog() != true)
            return;
        var secret = dlg.Input.Trim();
        if (secret.Length == 0)
            return;
        if (!AddRow(secret, _rows.Count == 0))
            MessageBox.Show("that key does not decode", "PumpCopy", MessageBoxButton.OK, MessageBoxImage.Warning);
    }

    private void OnRemoveClick(object sender, RoutedEventArgs e)
    {
        if (WalletGrid.SelectedItem is WalletRowVm row)
            _rows.Remove(row);
    }

    private async void OnRefreshClick(object sender, RoutedEventArgs e)
    {
        if (_rows.Count == 0)
            return;
        SavedHint.Text = "fetching balances...";
        try
        {
            var balances = await _host.GetBalancesAsync(_rows.Select(r => r.PubKey));
            foreach (var r in _rows)
                r.Balance = balances.TryGetValue(r.PubKey, out var b) && !double.IsNaN(b) ? $"{b:0.####} SOL" : "error";
            SavedHint.Text = $"balances {DateTime.Now:HH:mm:ss}";
        }
        catch (Exception ex)
        {
            SavedHint.Text = "rpc error";
            Core.Logging.Log.Warn($"balance refresh: {ex.Message}");
        }
    }

    private async void OnSellAllClick(object sender, RoutedEventArgs e)
    {
        if (MessageBox.Show("Sell every pump token the trading wallet holds?", "PumpCopy",
                MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes)
            return;

        SellAllButton.IsEnabled = false;
        SavedHint.Text = "selling...";
        await _host.SellAllAsync();
        SellAllButton.IsEnabled = true;
        OnRefreshClick(sender, e);
    }

    private void OnSaveClick(object sender, RoutedEventArgs e)
    {
        var c = _host.Config;
        c.MaskKeys = ShowKeysCheck.IsChecked != true;

        var secrets = _rows.Select(r => r.Secret).Where(s => s.Length > 0).ToList();
        WalletStore.Save(WalletStore.ResolvePath(c.KeyFile), secrets);
        c.TradingWallet = Math.Max(0, _rows.ToList().FindIndex(r => r.IsTrading));

        _host.ApplyConfig(c);
        _host.SaveConfig();
        SavedHint.Text = $"saved {DateTime.Now:HH:mm:ss}";
    }
}
