using System.IO;
using System.Windows;
using System.Windows.Threading;

namespace PumpCopy.App;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        DispatcherUnhandledException += (_, args) =>
        {
            Crash(args.Exception);
            args.Handled = true;
        };
        AppDomain.CurrentDomain.UnhandledException += (_, args) => Crash(args.ExceptionObject as Exception);
        // socket aborts on shutdown surface here; noisy, not a crash
        TaskScheduler.UnobservedTaskException += (_, args) =>
        {
            try
            {
                var dir = Path.Combine(AppContext.BaseDirectory, "logs");
                Directory.CreateDirectory(dir);
                File.AppendAllText(Path.Combine(dir, "crash.log"), $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] unobserved: {args.Exception}\n\n");
            }
            catch { }
            args.SetObserved();
        };
    }

    // werfault with no message is useless, dump it where we can read
    private static void Crash(Exception? ex)
    {
        var msg = ex?.ToString() ?? "unknown exception";
        try
        {
            var dir = Path.Combine(AppContext.BaseDirectory, "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "crash.log"), $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {msg}\n\n");
        }
        catch
        {
            // nowhere to write, at least show it
        }
        MessageBox.Show(msg, "PumpCopy crashed", MessageBoxButton.OK, MessageBoxImage.Error);
    }
}
