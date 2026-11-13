using AppA.Core;
using Shared.Core;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

app.MapGet("/vat/{country}", (string country) => InvoiceCalculator.VatRate(country));
app.MapGet("/total", () => InvoiceCalculator.Total(
    new[] { new Money(10m, "EUR"), new Money(5.5m, "EUR") }, "EUR").ToString());

app.Run();
