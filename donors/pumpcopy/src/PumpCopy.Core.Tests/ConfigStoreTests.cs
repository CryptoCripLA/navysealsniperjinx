using PumpCopy.Core.Config;

namespace PumpCopy.Core.Tests;

public class ConfigStoreTests
{
    private static string TempPath() =>
        Path.Combine(Path.GetTempPath(), $"pumpcopy-test-{Guid.NewGuid():n}.json");

    [Fact]
    public void SaveLoad_RoundTrip()
    {
        var path = TempPath();
        var cfg = new BotConfig
        {
            Rpcs = new List<string> { "https://rpc1.example.com" },
            FixedSol = 0.25m,
            SlippageBps = 750,
            Paper = false,
            Leaders = new List<LeaderConfig>
            {
                new() { Address = "11111111111111111111111111111111", Alias = "whale", Mult = 2.0m },
            },
        };

        new ConfigStore(path).Save(cfg);
        var loaded = new ConfigStore(path).Load();

        Assert.Equal("https://rpc1.example.com", loaded.Rpcs[0]);
        Assert.Equal(0.25m, loaded.FixedSol);
        Assert.Equal(750, loaded.SlippageBps);
        Assert.False(loaded.Paper);
        Assert.Single(loaded.Leaders);
        Assert.Equal("whale", loaded.Leaders[0].Alias);
        File.Delete(path);
    }

    [Fact]
    public void Load_PartialJson_FillsDefaults()
    {
        var path = TempPath();
        File.WriteAllText(path, "{ \"rpcs\": [ \"https://x\" ], \"slippageBps\": -5 }");

        var loaded = new ConfigStore(path).Load();

        Assert.Equal("https://x", loaded.Rpcs[0]);
        Assert.Equal(0, loaded.SlippageBps);
        Assert.True(loaded.Paper);
        Assert.Equal(0.1m, loaded.FixedSol);
        File.Delete(path);
    }

    [Fact]
    public void Load_BrokenJson_FallsBackToDefaults()
    {
        var path = TempPath();
        File.WriteAllText(path, "{ this is not json ");

        var loaded = new ConfigStore(path).Load();

        Assert.Equal(new BotConfig().Rpcs[0], loaded.Rpcs[0]);
        File.Delete(path);
        File.Delete(path + ".bad");
    }
}
