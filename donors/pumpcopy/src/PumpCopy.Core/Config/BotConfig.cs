namespace PumpCopy.Core.Config;

public class LeaderConfig
{
    public string Address { get; set; } = "";
    public string Alias { get; set; } = "";
    public bool Enabled { get; set; } = true;
    public decimal Mult { get; set; } = 1.0m;
}

public class BotConfig
{
    public int Version { get; set; } = 1;

    public List<string> Rpcs { get; set; } = new() { "https://api.mainnet-beta.solana.com" };
    public bool UseProxy { get; set; }
    // socks5://host:port or socks5://user:pass@host:port
    public List<string> Proxies { get; set; } = new();
    public int ActiveProxy { get; set; }

    public string KeyFile { get; set; } = "keys.txt";
    public int TradingWallet { get; set; }
    public bool MaskKeys { get; set; } = true;

    public bool Paper { get; set; } = true;
    public bool DemoFeed { get; set; } = true;

    // fixed | mult
    public string SizeMode { get; set; } = "fixed";
    public decimal FixedSol { get; set; } = 0.1m;
    public decimal Multiplier { get; set; } = 1.0m;
    public int SlippageBps { get; set; } = 500;
    public int SellSlippageBps { get; set; } = 1000;
    public ulong PriorityFee { get; set; } = 100000;

    public int MaxPositions { get; set; } = 5;
    public decimal MinLeaderSol { get; set; } = 0.5m;
    public decimal MaxOurSol { get; set; } = 1.0m;
    public bool CopySells { get; set; } = true;
    public bool RepeatBuys { get; set; }
    public bool SkipMigrated { get; set; } = true;
    public int DelayMs { get; set; }

    public bool Logging { get; set; } = true;
    public string LogLevel { get; set; } = "info";
    public bool VerboseLogs { get; set; }

    public List<LeaderConfig> Leaders { get; set; } = new();

    public BotConfig Clone()
    {
        var json = System.Text.Json.JsonSerializer.Serialize(this);
        return System.Text.Json.JsonSerializer.Deserialize<BotConfig>(json)!;
    }
}
