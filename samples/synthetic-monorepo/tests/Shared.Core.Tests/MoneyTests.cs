using Shared.Core;

namespace Shared.Core.Tests;

public class MoneyTests
{
    [Fact]
    public void Add_SameCurrency_SumsAmounts()
        => Assert.Equal(15.5m, new Money(10m, "EUR").Add(new Money(5.5m, "EUR")).Amount);

    [Fact]
    public void Amount_Setter_UpdatesValue()
    {
        var money = new Money(1m, "EUR") { Amount = 42m };
        Assert.Equal(42m, money.Amount);
    }

    [Fact]
    public void Constructor_SetsAmount()
        => Assert.Equal(7m, new Money(7m, "EUR").Amount);

    [Fact]
    public void Add_DifferentCurrency_Throws()
        => Assert.Throws<InvalidOperationException>(() => new Money(1m, "EUR").Add(new Money(1m, "USD")));
}
