using Solnet.Wallet;
using Solnet.Wallet.Bip39;

namespace PumpCopy.Core.Config;

public static class KeyFileLoader
{
    public static List<Account> LoadKeys(string path)
    {
        var result = new List<Account>();
        if (!File.Exists(path))
        {
            Logging.Log.Warn($"key file not found: {path}");
            return result;
        }

        int lineNo = 0;
        foreach (var raw in File.ReadAllLines(path))
        {
            lineNo++;
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith("#"))
                continue;

            // some people paste [12,34,..] json arrays from keygen files
            if (line.StartsWith("["))
            {
                try
                {
                    var bytes = System.Text.Json.JsonSerializer.Deserialize<byte[]>(line);
                    if (bytes != null && bytes.Length >= 64)
                    {
                        result.Add(Account.FromSecretKey(Solnet.Wallet.Utilities.Encoders.Base58.EncodeData(bytes)));
                        continue;
                    }
                }
                catch (Exception ex)
                {
                    Logging.Log.Warn($"key file line {lineNo}: bad json array ({ex.Message})");
                    continue;
                }
            }

            try
            {
                result.Add(Account.FromSecretKey(line));
            }
            catch (Exception ex)
            {
                Logging.Log.Warn($"key file line {lineNo}: skipped ({ex.Message})");
            }
        }

        return result;
    }
}
