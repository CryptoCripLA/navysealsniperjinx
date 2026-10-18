using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using PumpCopy.Core.Config;

namespace PumpCopy.App.Views;

public partial class TradingView : UserControl
{
    private readonly BotHost _host;
    private bool _loading;

    private static readonly ulong[] FeePresets = { 50_000, 100_000, 500_000, 1_000_000 };

    public TradingView(BotHost host)
    {
        InitializeComponent();
        _host = host;
        LoadFrom(host.Config);
    }

    public void LoadFrom(BotConfig c)
    {
        _loading = true;
        PaperCheck.IsChecked = c.Paper;
        SizeModeCombo.SelectedIndex = c.SizeMode == "mult" ? 1 : 0;
        FeeCombo.SelectedIndex = FeePresets.ToList().IndexOf(c.PriorityFee) is var i and >= 0 ? i : 1;
        FixedSolBox.Text = c.FixedSol.ToString(CultureInfo.InvariantCulture);
        MultBox.Text = c.Multiplier.ToString(CultureInfo.InvariantCulture);
        MaxOurSolBox.Text = c.MaxOurSol.ToString(CultureInfo.InvariantCulture);
        SlippageSlider.Value = c.SlippageBps / 100.0;
        MaxPosBox.Text = c.MaxPositions.ToString();
        MinLeaderBox.Text = c.MinLeaderSol.ToString(CultureInfo.InvariantCulture);
        DelayBox.Text = c.DelayMs.ToString();
        SellSlipBox.Text = c.SellSlippageBps.ToString();
        CopySellsCheck.IsChecked = c.CopySells;
        RepeatBuysCheck.IsChecked = c.RepeatBuys;
        SkipMigratedCheck.IsChecked = c.SkipMigrated;

        LoggingCheck.IsChecked = c.Logging;
        LevelCombo.SelectedIndex = c.LogLevel switch { "dbg" => 0, "warn" => 2, "err" => 3, _ => 1 };
        DemoFeedCheck.IsChecked = c.DemoFeed;
        VerboseCheck.IsChecked = c.VerboseLogs;
        _loading = false;
    }

    private void OnVerboseChanged(object sender, RoutedEventArgs e)
    {
        if (_loading || VerboseCheck == null)
            return;
        Core.Logging.Log.MinLevel = VerboseCheck.IsChecked == true
            ? Core.Logging.LogLevel.Dbg
            : LevelCombo.SelectedIndex switch { 0 => Core.Logging.LogLevel.Dbg, 2 => Core.Logging.LogLevel.Warn, 3 => Core.Logging.LogLevel.Err, _ => Core.Logging.LogLevel.Info };
    }

    private void OnSlippageChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (_loading || SlippageLabel == null)
            return;
        SlippageLabel.Text = $"slippage: {SlippageSlider.Value:0.#}%";
    }

    private void OnReloadClick(object sender, RoutedEventArgs e)
    {
        LoadFrom(_host.Config);
        SavedHint.Text = "reloaded from memory";
    }

    private void OnSaveClick(object sender, RoutedEventArgs e)
    {
        var c = _host.Config;
        c.Paper = PaperCheck.IsChecked == true;
        c.SizeMode = SizeModeCombo.SelectedIndex == 1 ? "mult" : "fixed";
        c.PriorityFee = FeeCombo.SelectedIndex is >= 0 and < 4 ? FeePresets[FeeCombo.SelectedIndex] : 100_000;
        c.FixedSol = ParseDec(FixedSolBox.Text, 0.1m);
        c.Multiplier = ParseDec(MultBox.Text, 1.0m);
        c.MaxOurSol = ParseDec(MaxOurSolBox.Text, 1.0m);
        c.SlippageBps = (int)Math.Round(SlippageSlider.Value * 100);
        c.MaxPositions = Math.Max(1, ParseInt(MaxPosBox.Text, 5));
        c.MinLeaderSol = ParseDec(MinLeaderBox.Text, 0.5m);
        c.DelayMs = Math.Max(0, ParseInt(DelayBox.Text, 0));
        c.SellSlippageBps = Math.Max(0, ParseInt(SellSlipBox.Text, 1000));
        c.CopySells = CopySellsCheck.IsChecked == true;
        c.RepeatBuys = RepeatBuysCheck.IsChecked == true;
        c.SkipMigrated = SkipMigratedCheck.IsChecked == true;

        c.Logging = LoggingCheck.IsChecked == true;
        c.LogLevel = LevelCombo.SelectedIndex switch { 0 => "dbg", 2 => "warn", 3 => "err", _ => "info" };
        c.DemoFeed = DemoFeedCheck.IsChecked == true;
        c.VerboseLogs = VerboseCheck.IsChecked == true;

        _host.ApplyConfig(c);
        _host.SaveConfig();
        SavedHint.Text = $"saved {DateTime.Now:HH:mm:ss}";
        Core.Logging.Log.Info("trading config saved");
    }

    private static decimal ParseDec(string s, decimal def)
        => decimal.TryParse(s.Replace(',', '.'), NumberStyles.Any, CultureInfo.InvariantCulture, out var v) ? v : def;

    private static int ParseInt(string s, int def)
        => int.TryParse(s, out var v) ? v : def;
}


