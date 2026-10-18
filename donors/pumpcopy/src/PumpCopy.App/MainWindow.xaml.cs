using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Media;
using System.Windows.Threading;
using PumpCopy.App.ViewModels;
using PumpCopy.App.Views;
using PumpCopy.Core.Config;
using PumpCopy.Core.Logging;

namespace PumpCopy.App;

public class LogLineVm
{
    public string Line { get; init; } = "";
    public Brush Brush { get; init; } = Brushes.Gray;
}

public partial class MainWindow : Window
{
    private const int LogCap = 2000;
    private readonly ObservableCollection<LogLineVm> _logLines = new();
    private readonly BotHost _host;
    private readonly MainViewModel _vm = new();
    private readonly DispatcherTimer _statusTimer;
    private bool _starting;

    private static readonly SolidColorBrush InfoBrush = new(Color.FromRgb(0xD6, 0xD9, 0xDD));
    private static readonly SolidColorBrush WarnBrush = new(Color.FromRgb(0xE8, 0xB3, 0x3E));
    private static readonly SolidColorBrush ErrBrush = new(Color.FromRgb(0xEF, 0x53, 0x50));
    private static readonly SolidColorBrush DbgBrush = new(Color.FromRgb(0x8B, 0x90, 0x96));
    private static readonly SolidColorBrush BuyBrush = new(Color.FromRgb(0x23, 0xD5, 0xA0));

    public MainWindow()
    {
        InitializeComponent();

        var store = new ConfigStore();
        _host = new BotHost(store, store.Load());

        LogView.ItemsSource = _logLines;
        Log.Entry += OnLogEntry;

        MainHost.Content = new MainView { DataContext = _vm };
        WalletHost.Content = new WalletView(_host);
        ConnectionHost.Content = new ConnectionView(_host);
        TradingHost.Content = new TradingView(_host);
        _vm.SyncLeaders(_host.Config.Leaders);
        _vm.AddLeaderRequested += OnAddLeader;
        _vm.RemoveLeaderRequested += OnRemoveLeader;
        _vm.LeaderToggled += cfg =>
        {
            var existing = _host.Config.Leaders.FirstOrDefault(l => l.Address == cfg.Address);
            if (existing != null) existing.Enabled = cfg.Enabled;
            _host.SaveConfig();
        };

        _host.Record += r =>
        {
            _vm.OnRecord(r);
        };
        _host.StatsChanged += () => Dispatcher.BeginInvoke(RefreshStats);
        _host.ConnectionChanged += ok => Dispatcher.BeginInvoke(() =>
        {
            StatusDot.Fill = ok ? BuyBrush : ErrBrush;
            WsStatusText.Text = ok ? "ws: connected" : "ws: down";
        });

        _statusTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _statusTimer.Tick += (_, _) => RefreshStats();
        _statusTimer.Start();

        ModeBadgeText.Text = _host.Config.Paper ? "PAPER" : "LIVE";
        ModeBadge.Background = _host.Config.Paper ? WarnBrush : BuyBrush;
        VersionText.Text = "v" + (typeof(MainWindow).Assembly.GetName().Version?.ToString(3) ?? "0.1.0");
        Log.Info("ui ready");

        // headless runs and screenshot sessions
        if (Environment.GetCommandLineArgs().Contains("--autostart"))
            Dispatcher.BeginInvoke(new Action(() => OnStartClick(this, new RoutedEventArgs())));
    }

    private void RefreshStats()
    {
        _vm.Pnl = _host.Positions.SessionPnlLamports / 1e9;
        PositionsText.Text = $"positions: {_host.Positions.Open.Count}";
        PnlText.Text = $"session pnl: {_host.Positions.SessionPnlLamports / 1e9:+0.####;-0.####;0.0000} SOL";
        RpcStatusText.Text = _host.Pool == null
            ? "rpc: -"
            : $"rpc: {_host.Pool.LastLatencyMs} ms ({(_host.Pool.ActiveUrl.Length > 26 ? _host.Pool.ActiveUrl[..26] + "..." : _host.Pool.ActiveUrl)})";
    }

    private void OnAddLeader()
    {
        var dlg = new AddLeaderDialog { Owner = this };
        if (dlg.ShowDialog() != true)
            return;

        var cfg = new LeaderConfig { Address = dlg.Address, Alias = dlg.Alias, Mult = dlg.Mult };
        _host.Config.Leaders.Add(cfg);
        _host.SaveConfig();
        _vm.SyncLeaders(_host.Config.Leaders);
        Log.Info($"leader added: {dlg.Alias} {dlg.Address[..8]}...");
    }

    private void OnRemoveLeader(LeaderRowVm? row)
    {
        if (row == null)
            return;
        _host.Config.Leaders.RemoveAll(l => l.Address == row.Address);
        _host.SaveConfig();
        _vm.SyncLeaders(_host.Config.Leaders);
        Log.Info($"leader removed: {row.Address[..8]}...");
    }

    private void OnLogEntry(LogEntry entry)
    {
        var line = new LogLineVm
        {
            Line = $"[{entry.Time:HH:mm:ss}] {entry.Message}",
            Brush = PickBrush(entry),
        };

        Dispatcher.BeginInvoke(() =>
        {
            _logLines.Add(line);
            if (_logLines.Count > LogCap)
                _logLines.RemoveAt(0);
            if (AutoscrollCheck.IsChecked == true && _logLines.Count > 0)
                LogView.ScrollIntoView(_logLines[_logLines.Count - 1]);
        });
    }

    private static Brush PickBrush(LogEntry e)
    {
        return e.Level switch
        {
            LogLevel.Err => ErrBrush,
            LogLevel.Warn => WarnBrush,
            LogLevel.Dbg => DbgBrush,
            // copies and leader fills read nicer in accent
            LogLevel.Info when e.Message.StartsWith("copy [") || e.Message.StartsWith("leader trade") => BuyBrush,
            _ => InfoBrush,
        };
    }

    private void OnClearLogClick(object sender, RoutedEventArgs e) => _logLines.Clear();

    private async void OnStartClick(object sender, RoutedEventArgs e)
    {
        if (_starting || _host.Running)
            return;
        _starting = true;
        StartButton.IsEnabled = false;
        StatusText.Text = "starting...";

        var err = await _host.StartAsync();
        _starting = false;

        if (err != null)
        {
            StatusText.Text = err;
            StatusDot.Fill = ErrBrush;
            StartButton.IsEnabled = true;
            Log.Err($"start failed: {err}");
            return;
        }

        StatusText.Text = "running";
        StatusDot.Fill = BuyBrush;
        StopButton.IsEnabled = true;
    }

    private async void OnStopClick(object sender, RoutedEventArgs e)
    {
        StopButton.IsEnabled = false;
        await _host.StopAsync();
        StatusText.Text = "stopped";
        StatusDot.Fill = ErrBrush;
        WsStatusText.Text = "ws: -";
        StartButton.IsEnabled = true;
    }

    protected override void OnClosed(EventArgs e)
    {
        _statusTimer.Stop();
        Log.Entry -= OnLogEntry;
        Log.Flush();
        base.OnClosed(e);
    }
}
