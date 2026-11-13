using AppA.Core;
using Shared.Core;

namespace AppA.Tests;

public class InvoiceCalculatorTests
{
    [Fact]
    public void Total_SumsAllLines()
        => Assert.Equal(15.5m, InvoiceCalculator.Total(new[] { new Money(10m, "EUR"), new Money(5.5m, "EUR") }, "EUR").Amount);

    [Fact]
    public void VatRate_Czechia()
        => Assert.Equal(0.21m, InvoiceCalculator.VatRate("CZ"));

    // Pre-existing failure, unrelated to any migration: the expected rate is out of date.
    // The migration skill must record it as a baseline failure and never "fix" it.
    [Fact]
    public void VatRate_Germany_KnownBaselineFailure()
        => Assert.Equal(0.16m, InvoiceCalculator.VatRate("DE"));
}
