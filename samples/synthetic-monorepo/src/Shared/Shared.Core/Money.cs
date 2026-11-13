namespace Shared.Core;

/// <summary>An amount in a currency. Shared by AppA, AppB and LegacyAdmin.</summary>
public sealed class Money
{
    // A field literally named "field". C# 14 (the default for net10.0) makes `field`
    // inside a property accessor a keyword, so this code changes meaning there.
    private decimal field;

    public Money(decimal amount, string currency)
    {
        field = amount;
        Currency = currency;
    }

    public decimal Amount
    {
        get { return field; }
        set { field = value; }
    }

    public string Currency { get; }

    public Money Add(Money other)
    {
        if (other.Currency != Currency)
            throw new InvalidOperationException("Currency mismatch.");
        return new Money(Amount + other.Amount, Currency);
    }

    public override string ToString() => $"{Amount:0.00} {Currency}";
}
