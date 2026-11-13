using AppB.Core;
using Microsoft.Extensions.Hosting;

var host = Host.CreateApplicationBuilder(args).Build();
var large = await PaymentStream.LargePayments(PaymentStream.Read(5m, 150m, 20m, 999m), 100m);
Console.WriteLine($"Large payments: {large.Count}");
await host.StopAsync();
