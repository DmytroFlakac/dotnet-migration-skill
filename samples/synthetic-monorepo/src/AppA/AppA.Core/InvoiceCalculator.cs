using Shared.Core;

namespace AppA.Core;

public static class InvoiceCalculator
{
    public static Money Total(IEnumerable<Money> lines, string currency)
    {
        var total = new Money(0m, currency);
        foreach (var line in lines)
            total = total.Add(line);
        return total;
    }

    public static decimal VatRate(string country) => country switch
    {
        "CZ" => 0.21m,
        "DE" => 0.19m,
        _ => 0.20m,
    };
}
