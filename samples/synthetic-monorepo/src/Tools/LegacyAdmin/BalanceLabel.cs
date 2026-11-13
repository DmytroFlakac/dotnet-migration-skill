using System.Windows.Forms;
using Shared.Core;

namespace LegacyAdmin;

public sealed class BalanceLabel : Label
{
    public void Show(Money balance) => Text = balance.ToString();
}
