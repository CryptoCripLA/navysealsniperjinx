using Solnet.Wallet;

namespace PumpCopy.Core.Config;

public static class WalletStore
{
    public static List<Account> Load(string path) => KeyFileLoader.LoadKeys(path);

    public static void Save(string path, IEnumerable<string> secrets)
    {
        var dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir))
            Directory.CreateDirectory(dir);
        File.WriteAllLines(path, secrets);
    }

    public static string ResolvePath(string path)
        => Path.IsPathRooted(path) ? path : Path.Combine(AppContext.BaseDirectory, path);

    // show first4....last4, never the middle
    public static string Mask(string secret)
    {
        if (secret.Length <= 10)
            return new string('*', secret.Length);
        return secret[..4] + new string('*', 16) + secret[^4..];
    }
}
