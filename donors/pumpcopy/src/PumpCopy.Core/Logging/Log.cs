namespace PumpCopy.Core.Logging;

public enum LogLevel
{
    Dbg,
    Info,
    Warn,
    Err,
}

public record LogEntry(DateTime Time, LogLevel Level, string Message);

public static class Log
{
    private static readonly object FileLock = new();
    private static string _logDir = "";
    private static string _currentDay = "";
    private static StreamWriter? _writer;

    public static LogLevel MinLevel { get; set; } = LogLevel.Info;
    public static bool FileEnabled { get; set; } = true;

    public static event Action<LogEntry>? Entry;

    public static void Init(string? logDir = null, LogLevel min = LogLevel.Info)
    {
        _logDir = logDir ?? System.IO.Path.Combine(AppContext.BaseDirectory, "logs");
        MinLevel = min;
    }

    public static void Dbg(string msg) => Write(LogLevel.Dbg, msg);
    public static void Info(string msg) => Write(LogLevel.Info, msg);
    public static void Warn(string msg) => Write(LogLevel.Warn, msg);
    public static void Err(string msg) => Write(LogLevel.Err, msg);

    public static void Flush()
    {
        lock (FileLock)
        {
            _writer?.Flush();
        }
    }

    private static void Write(LogLevel level, string msg)
    {
        if (level < MinLevel)
            return;

        var entry = new LogEntry(DateTime.Now, level, msg);
        Entry?.Invoke(entry);

        // file sink only after Init(), until then event subscribers only
        if (!FileEnabled || string.IsNullOrEmpty(_logDir))
            return;

        lock (FileLock)
        {
            try
            {
                var day = entry.Time.ToString("yyyyMMdd");
                if (day != _currentDay || _writer == null)
                {
                    _writer?.Dispose();
                    if (!string.IsNullOrEmpty(_logDir))
                        Directory.CreateDirectory(_logDir);
                    var path = System.IO.Path.Combine(_logDir, $"pumpcopy-{day}.log");
                    _writer = new StreamWriter(new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read))
                    {
                        AutoFlush = false,
                    };
                    _currentDay = day;
                }

                _writer.WriteLine($"[{entry.Time:HH:mm:ss}] [{level,-4}] {msg}");
                _writer.Flush();
            }
            catch
            {
                // disk full / locked dir: ui still gets the entry, file log dies silently
            }
        }
    }
}
