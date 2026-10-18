using System.Windows;

namespace PumpCopy.App.Views;

public partial class PromptDialog : Window
{
    public string Input => InputBox.Text;

    public PromptDialog(string title, string label, string initial = "")
    {
        InitializeComponent();
        Title = title;
        LabelText.Text = label;
        InputBox.Text = initial;
    }

    private void OnOkClick(object sender, RoutedEventArgs e)
    {
        DialogResult = true;
        Close();
    }
}
