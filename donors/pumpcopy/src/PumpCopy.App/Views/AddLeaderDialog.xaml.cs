using System.Windows;
using Solnet.Wallet;

namespace PumpCopy.App.Views;

public partial class AddLeaderDialog : Window
{
    public string Alias { get; private set; } = "";
    public string Address { get; private set; } = "";
    public decimal Mult { get; private set; } = 1.0m;

    public AddLeaderDialog()
    {
        InitializeComponent();
    }

    private void OnOkClick(object sender, RoutedEventArgs e)
    {
        var addr = AddressBox.Text.Trim();
        if (addr.Length == 0)
        {
            MessageBox.Show(this, "Wallet address is empty", "PumpCopy", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        try
        {
            _ = new PublicKey(addr);
        }
        catch
        {
            MessageBox.Show(this, "That does not look like a base58 solana address", "PumpCopy", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        if (!decimal.TryParse(MultBox.Text.Replace(',', '.'), out var m) || m <= 0)
            m = 1.0m;

        Alias = AliasBox.Text.Trim();
        Address = addr;
        Mult = m;
        DialogResult = true;
        Close();
    }
}
