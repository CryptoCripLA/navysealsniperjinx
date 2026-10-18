namespace PumpCopy.Core.Copy;

public class Position
{
    public string Mint { get; init; } = "";
    public ulong Tokens { get; set; }
    public ulong CostLamports { get; set; }
    public ulong ProceedsLamports { get; set; }
    public ulong LeaderTokens { get; set; }
    public DateTime OpenedAt { get; init; } = DateTime.Now;
}

public class PositionStore
{
    private readonly Dictionary<string, Position> _open = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<Position> _closed = new();

    public event Action? Changed;

    public IReadOnlyCollection<Position> Open => _open.Values.ToList();
    public IReadOnlyList<Position> Closed => _closed;
    public long SessionPnlLamports { get; private set; }

    // sells keep their chunk so a failed tx can be rolled back
    private readonly Dictionary<string, (ulong Tokens, ulong Proceeds, ulong CostChunk)> _lastSell = new();

    public Position? Get(string mint) => _open.TryGetValue(mint, out var p) ? p : null;

    public void ApplyBuy(string mint, ulong tokens, ulong solLamports, ulong leaderTokens)
    {
        if (!_open.TryGetValue(mint, out var p))
        {
            p = new Position { Mint = mint };
            _open[mint] = p;
        }

        p.Tokens += tokens;
        p.CostLamports += solLamports;
        p.LeaderTokens += leaderTokens;
        Changed?.Invoke();
    }

    // returns realized pnl lamports for this sell chunk
    public long ApplySell(string mint, ulong tokens, ulong solLamports, ulong leaderTokensOut)
    {
        if (!_open.TryGetValue(mint, out var p))
            return 0;

        p.Tokens = p.Tokens > tokens ? p.Tokens - tokens : 0;
        p.ProceedsLamports += solLamports;
        p.LeaderTokens = p.LeaderTokens > leaderTokensOut ? p.LeaderTokens - leaderTokensOut : 0;

        // pnl attributed proportionally to the sold chunk
        ulong costOfChunk = p.Tokens + tokens == 0 ? 0 : p.CostLamports * tokens / (p.Tokens + tokens);
        p.CostLamports -= costOfChunk;
        long pnl = (long)solLamports - (long)costOfChunk;
        SessionPnlLamports += pnl;

        if (p.Tokens == 0)
        {
            _open.Remove(mint);
            _closed.Add(p);
            if (_closed.Count > 200) _closed.RemoveAt(0);
        }

        _lastSell[mint] = (tokens, solLamports, costOfChunk);
        Changed?.Invoke();
        return pnl;
    }

    public void RevertBuy(string mint, ulong tokens, ulong solLamports)
    {
        if (!_open.TryGetValue(mint, out var p))
            return;
        p.Tokens = p.Tokens > tokens ? p.Tokens - tokens : 0;
        p.CostLamports = p.CostLamports > solLamports ? p.CostLamports - solLamports : 0;
        if (p.Tokens == 0)
            _open.Remove(mint);
        Changed?.Invoke();
    }

    public void RevertSell(string mint)
    {
        if (!_lastSell.TryGetValue(mint, out var last))
            return;
        _lastSell.Remove(mint);

        if (!_open.TryGetValue(mint, out var p))
        {
            p = new Position { Mint = mint };
            _open[mint] = p;
        }
        p.Tokens += last.Tokens;
        p.CostLamports += last.CostChunk;
        p.ProceedsLamports = p.ProceedsLamports > last.Proceeds ? p.ProceedsLamports - last.Proceeds : 0;
        SessionPnlLamports -= (long)last.Proceeds - (long)last.CostChunk;
        Changed?.Invoke();
    }
}
