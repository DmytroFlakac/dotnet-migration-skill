using Shared.Core;

namespace AppB.Core;

public static class PaymentStream
{
    public static async IAsyncEnumerable<Money> Read(params decimal[] amounts)
    {
        foreach (var amount in amounts)
        {
            await Task.Yield();
            yield return new Money(amount, "EUR");
        }
    }

    // LINQ over IAsyncEnumerable from the System.Linq.Async package.
    // .NET 10 ships the same operators in the base library (System.Linq.AsyncEnumerable).
    public static async Task<List<Money>> LargePayments(IAsyncEnumerable<Money> payments, decimal threshold)
        => await payments.Where(p => p.Amount >= threshold).ToListAsync();
}
