"""Writes the synthetic monorepo used as the acceptance test.

Run once: python samples/make_sample.py  ->  samples/synthetic-monorepo/
Kept as a generator so the sample's intent is documented in one place.
"""
from pathlib import Path

ROOT = Path(__file__).parent / "synthetic-monorepo"

TEST_PROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <IsPackable>false</IsPackable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" />
    <PackageReference Include="xunit" />
    <PackageReference Include="xunit.runner.visualstudio" />
  </ItemGroup>
  <ItemGroup>
    <Using Include="Xunit" />
    <ProjectReference Include="{ref}" />
  </ItemGroup>
</Project>
"""

FILES = {
    "global.json": """{
  "sdk": {
    "version": "10.0.100",
    "rollForward": "latestFeature"
  }
}
""",
    "Directory.Build.props": """<Project>
  <PropertyGroup>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <Company>Synthetic Monorepo Ltd</Company>
  </PropertyGroup>
</Project>
""",
    "Directory.Packages.props": """<Project>
  <PropertyGroup>
    <ManagePackageVersionsCentrally>true</ManagePackageVersionsCentrally>
  </PropertyGroup>
  <ItemGroup>
    <PackageVersion Include="Microsoft.Extensions.Hosting" Version="8.0.1" />
    <PackageVersion Include="System.Linq.Async" Version="6.0.1" />
    <PackageVersion Include="Microsoft.NET.Test.Sdk" Version="17.14.1" />
    <PackageVersion Include="xunit" Version="2.9.3" />
    <PackageVersion Include="xunit.runner.visualstudio" Version="3.1.4" />
  </ItemGroup>
</Project>
""",
    "NuGet.config": """<?xml version="1.0" encoding="utf-8"?>
<!-- No feed is hard-coded here: the machine's own NuGet configuration applies (organisation mirror,
     credential provider, proxy). The test suite can point the sample at one feed with the
     DMIG_NUGET_SOURCE environment variable (a URL or a local folder). -->
<configuration>
  <packageSources>
  </packageSources>
</configuration>
""",
    ".gitlab-ci.yml": """image: mcr.microsoft.com/dotnet/sdk:8.0

stages: [build, test]

build:
  stage: build
  script:
    - dotnet build Monorepo.sln -c Release

test:
  stage: test
  script:
    - dotnet test Monorepo.sln -c Release
""",
    ".gitignore": "bin/\nobj/\nTestResults/\n",
    # ---------------------------------------------------------------- Shared.Core
    "src/Shared/Shared.Core/Shared.Core.csproj": """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
""",
    "src/Shared/Shared.Core/Money.cs": """namespace Shared.Core;

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
""",
    # ---------------------------------------------------------------- AppA
    "src/AppA/AppA.Core/AppA.Core.csproj": """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\..\\Shared\\Shared.Core\\Shared.Core.csproj" />
  </ItemGroup>
</Project>
""",
    "src/AppA/AppA.Core/InvoiceCalculator.cs": """using Shared.Core;

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
""",
    "src/AppA/AppA.Web/AppA.Web.csproj": """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\AppA.Core\\AppA.Core.csproj" />
  </ItemGroup>
</Project>
""",
    "src/AppA/AppA.Web/Program.cs": """using AppA.Core;
using Shared.Core;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

app.MapGet("/vat/{country}", (string country) => InvoiceCalculator.VatRate(country));
app.MapGet("/total", () => InvoiceCalculator.Total(
    new[] { new Money(10m, "EUR"), new Money(5.5m, "EUR") }, "EUR").ToString());

app.Run();
""",
    "src/AppA/AppA.Web/Dockerfile": """FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY publish/ .
ENTRYPOINT ["dotnet", "AppA.Web.dll"]
""",
    # ---------------------------------------------------------------- AppB
    "src/AppB/AppB.Core/AppB.Core.csproj": """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="System.Linq.Async" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="..\\..\\Shared\\Shared.Core\\Shared.Core.csproj" />
  </ItemGroup>
</Project>
""",
    "src/AppB/AppB.Core/PaymentStream.cs": """using Shared.Core;

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
""",
    "src/AppB/AppB.Worker/AppB.Worker.csproj": """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.Extensions.Hosting" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="..\\AppB.Core\\AppB.Core.csproj" />
  </ItemGroup>
</Project>
""",
    "src/AppB/AppB.Worker/Program.cs": """using AppB.Core;
using Microsoft.Extensions.Hosting;

var host = Host.CreateApplicationBuilder(args).Build();
var large = await PaymentStream.LargePayments(PaymentStream.Read(5m, 150m, 20m, 999m), 100m);
Console.WriteLine($"Large payments: {large.Count}");
await host.StopAsync();
""",
    # ---------------------------------------------------------------- Tools (Windows only)
    "src/Tools/LegacyAdmin/LegacyAdmin.csproj": """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0-windows</TargetFramework>
    <UseWindowsForms>true</UseWindowsForms>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\..\\Shared\\Shared.Core\\Shared.Core.csproj" />
  </ItemGroup>
</Project>
""",
    "src/Tools/LegacyAdmin/BalanceLabel.cs": """using System.Windows.Forms;
using Shared.Core;

namespace LegacyAdmin;

public sealed class BalanceLabel : Label
{
    public void Show(Money balance) => Text = balance.ToString();
}
""",
    # ---------------------------------------------------------------- tests
    "tests/Shared.Core.Tests/Shared.Core.Tests.csproj": TEST_PROJ.format(ref="..\\..\\src\\Shared\\Shared.Core\\Shared.Core.csproj"),
    "tests/AppA.Tests/AppA.Tests.csproj": TEST_PROJ.format(ref="..\\..\\src\\AppA\\AppA.Core\\AppA.Core.csproj"),
    "tests/AppB.Tests/AppB.Tests.csproj": TEST_PROJ.format(ref="..\\..\\src\\AppB\\AppB.Core\\AppB.Core.csproj"),
    "tests/Shared.Core.Tests/MoneyTests.cs": """using Shared.Core;

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
""",
    "tests/AppA.Tests/InvoiceCalculatorTests.cs": """using AppA.Core;
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
""",
    "tests/AppB.Tests/PaymentStreamTests.cs": """using AppB.Core;

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
""",
    "AppA.slnf": """{
  "solution": {
    "path": "Monorepo.sln",
    "projects": [
      "src\\\\AppA\\\\AppA.Core\\\\AppA.Core.csproj",
      "src\\\\AppA\\\\AppA.Web\\\\AppA.Web.csproj",
      "src\\\\Shared\\\\Shared.Core\\\\Shared.Core.csproj",
      "tests\\\\AppA.Tests\\\\AppA.Tests.csproj"
    ]
  }
}
""",
}


def main() -> None:
    for rel, text in FILES.items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(FILES)} files to {ROOT}")


if __name__ == "__main__":
    main()
