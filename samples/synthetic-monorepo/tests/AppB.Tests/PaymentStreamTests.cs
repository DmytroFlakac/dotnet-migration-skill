using AppB.Core;

namespace AppB.Tests;

public class PaymentStreamTests
{
    [Fact]
    public async Task LargePayments_FiltersByThreshold()
    {
        var large = await PaymentStream.LargePayments(PaymentStream.Read(5m, 150m, 20m, 999m), 100m);
        Assert.Equal(2, large.Count);
    }
}
