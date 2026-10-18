using System.Collections.ObjectModel;
using System.Windows;
using PumpCopy.Core.Config;
using PumpCopy.Core.Copy;

namespace PumpCopy.App.ViewModels;

public class LeaderRowVm : ViewModelBase
{
    private bool _enabled;
    private int _copied;
    private string _last = "-";

    public string Address { get; init; } = "";
    public string Alias { get; init; } = "";
    public decimal Mult { get; init; } = 1.0m;

    public bool Enabled
    {
        get => _enabled;
        set => Set(ref _enabled, value);
    }

    public int Copied
    {
        get => _copied;
        set => Set(ref _copied, value);
    }

    public string Last
    {
        get => _last;
        set => Set(ref _last, value);
    }

    public string ShortAddress => Address.Length > 12 ? Address[..6] + "..." + Address[^4..] : Address;
}

public class FeedRowVm
{
    public string Time { get; init; } = "";
    public string Leader { get; init; } = "";
    public string Side { get; init; } = "";
    public bool IsBuy { get; init; }
    public string Mint { get; init; } = "";
    public string LeaderSol { get; init; } = "";
    public string OurSol { get; init; } = "";
    public string Status { get; init; } = "";
}

public class MainViewModel : ViewModelBase
{
    private const int FeedCap = 500;

    private int _copied;
    private int _skipped;
    private int _errors;
    private double _pnl;

    public ObservableCollection<LeaderRowVm> Leaders { get; } = new();
    public ObservableCollection<FeedRowVm> Feed { get; } = new();

    public int Copied { get => _copied; set => Set(ref _copied, value); }
    public int Skipped { get => _skipped; set => Set(ref _skipped, value); }
    public int Errors { get => _errors; set => Set(ref _errors, value); }
    public double Pnl { get => _pnl; set => Set(ref _pnl, value); }

    public event Action<LeaderConfig>? LeaderToggled;
    public event Action? AddLeaderRequested;
    public event Action<LeaderRowVm?>? RemoveLeaderRequested;

    public RelayCommand AddLeaderCommand { get; }
    public RelayCommand RemoveLeaderCommand { get; }

    private LeaderRowVm? _selectedLeader;

    public LeaderRowVm? SelectedLeader
    {
        get => _selectedLeader;
        set => Set(ref _selectedLeader, value);
    }

    public MainViewModel()
    {
        AddLeaderCommand = new RelayCommand(_ => AddLeaderRequested?.Invoke());
        RemoveLeaderCommand = new RelayCommand(_ => RemoveLeaderRequested?.Invoke(SelectedLeader));
    }

    public void SyncLeaders(IEnumerable<LeaderConfig> leaders)
    {
        Leaders.Clear();
        foreach (var l in leaders)
        {
            var row = new LeaderRowVm { Address = l.Address, Alias = l.Alias, Mult = l.Mult, Enabled = l.Enabled };
            row.PropertyChanged += (_, e) =>
            {
                if (e.PropertyName == nameof(LeaderRowVm.Enabled))
                    LeaderToggled?.Invoke(new LeaderConfig { Address = row.Address, Alias = row.Alias, Mult = row.Mult, Enabled = row.Enabled });
            };
            Leaders.Add(row);
        }
    }

    public void OnRecord(CopyRecord r)
    {
        Application.Current?.Dispatcher.BeginInvoke(() =>
        {
            if (r.Status.StartsWith("skipped"))
                Skipped++;
            else if (r.Status.Contains("error") || r.Status.Contains("fail"))
                Errors++;
            else
                Copied++;

            var row = Leaders.FirstOrDefault(l => l.Address.StartsWith(r.Leader) || l.Alias == r.Leader);
            if (row != null && !r.Status.StartsWith("skipped"))
            {
                row.Copied++;
                row.Last = DateTime.Now.ToString("HH:mm:ss");
            }

            Feed.Add(new FeedRowVm
            {
                Time = r.Time.ToString("HH:mm:ss"),
                Leader = r.Leader,
                Side = r.IsBuy ? "BUY" : "SELL",
                IsBuy = r.IsBuy,
                Mint = r.Mint.Length > 12 ? r.Mint[..6] + "..." + r.Mint[^4..] : r.Mint,
                LeaderSol = r.LeaderSol.ToString("0.####"),
                OurSol = r.OurSol > 0 ? r.OurSol.ToString("0.####") : "-",
                Status = r.Status,
            });
            if (Feed.Count > FeedCap)
                Feed.RemoveAt(0);
        });
    }
}
