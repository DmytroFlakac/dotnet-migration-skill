# Sources of the breaking-change catalogue

Downloaded 2026-11-13 from Microsoft's public documentation repositories (CC BY 4.0 / MIT):

| File | Source |
|---|---|
| `bc-9.0.md`, `bc-10.md` | https://github.com/dotnet/docs — `docs/core/compatibility/9.0.md`, `10.md` |
| `asp-9.md`, `asp-10.md` | https://github.com/dotnet/AspNetCore.Docs — `aspnetcore/breaking-changes/9/overview.md`, `10/overview.md` |
| `ef-9.md`, `ef-10.md` | https://github.com/dotnet/EntityFramework.Docs — `entity-framework/core/what-is-new/ef-core-9.0/breaking-changes.md`, `ef-core-10.0/…` |
| `syslib.md` | https://github.com/dotnet/docs — `docs/fundamentals/syslib-diagnostics/obsoletions-overview.md` |

`python recipes/build_catalog.py` turns them into `recipes/rules/catalog.yaml`. Refresh by downloading
newer copies and rerunning it; the generated file is committed so runs never need the network.
