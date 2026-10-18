using System.Text.Json;
using System.Text.Json.Serialization;

namespace PumpCopy.Core.Config;

public class ConfigStore
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    public string Path { get; }

    public ConfigStore(string? path = null)
    {
        Path = path ?? System.IO.Path.Combine(AppContext.BaseDirectory, "config.json");
    }

    public BotConfig Load()
    {
        if (!File.Exists(Path))
        {
            var def = new BotConfig();
            Save(def);
            return def;
        }

        try
        {
            var raw = File.ReadAllText(Path);
            var cfg = JsonSerializer.Deserialize<BotConfig>(raw, JsonOpts);
            if (cfg == null)
                return new BotConfig();
            Sanitize(cfg);
            return cfg;
        }
        catch (Exception ex)
        {
            // broken config should not kill startup, keep a backup
            try { File.Copy(Path, Path + ".bad", true); } catch { }
            Logging.Log.Err($"config.json unreadable ({ex.Message}), using defaults");
            return new BotConfig();
        }
    }

    public void Save(BotConfig cfg)
    {
        var dir = System.IO.Path.GetDirectoryName(Path);
        if (!string.IsNullOrEmpty(dir))
            Directory.CreateDirectory(dir);
        File.WriteAllText(Path, JsonSerializer.Serialize(cfg, JsonOpts));
    }

    private static void Sanitize(BotConfig cfg)
    {
        if (cfg.FixedSol <= 0) cfg.FixedSol = 0.1m;
        if (cfg.Multiplier <= 0) cfg.Multiplier = 1.0m;
        if (cfg.SlippageBps < 0) cfg.SlippageBps = 0;
        if (cfg.SlippageBps > 9900) cfg.SlippageBps = 9900;
        if (cfg.SellSlippageBps < 0) cfg.SellSlippageBps = 0;
        if (cfg.SellSlippageBps > 9900) cfg.SellSlippageBps = 9900;
        if (cfg.MaxPositions < 1) cfg.MaxPositions = 1;
        if (cfg.DelayMs < 0) cfg.DelayMs = 0;
        cfg.Leaders ??= new List<LeaderConfig>();
        cfg.Rpcs ??= new List<string>();
        cfg.Rpcs.RemoveAll(r => string.IsNullOrWhiteSpace(r));
        if (cfg.Rpcs.Count == 0)
            cfg.Rpcs.Add("https://api.mainnet-beta.solana.com");
        cfg.Proxies ??= new List<string>();
        cfg.Proxies.RemoveAll(p => string.IsNullOrWhiteSpace(p));
        if (cfg.ActiveProxy < 0 || cfg.ActiveProxy >= cfg.Proxies.Count) cfg.ActiveProxy = 0;
        if (cfg.TradingWallet < 0) cfg.TradingWallet = 0;
    }
}

