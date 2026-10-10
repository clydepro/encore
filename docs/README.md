# Encore Documentation Map

Where to look, and which document is authoritative when two disagree.

## Planning documents (authoritative, written before code)

| Document | Path | Role |
| -------- | ---- | ---- |
| SAPRS | [`docs/SAPRS/Encore-SAPRS.md`](SAPRS/Encore-SAPRS.md) | The architectural blueprint. Highest authority after the task request (AEP 2). |
| AIG | [`docs/AIG/AIImplementationGuide_AIG.md`](AIG/AIImplementationGuide_AIG.md) | The implementation contract: what to build, in what order. |
| AEP | [`docs/AEP/AIEngineeringPlaybook_AEP.md`](AEP/AIEngineeringPlaybook_AEP.md) | Engineering standards and workflow for every change. |
| PBK | [`docs/ProjectBootstrapKit_PBK.md`](ProjectBootstrapKit_PBK.md) | Repository and project initialization — this phase. |

Those four files are vendored inputs; they are excluded from Markdown linting so
their original wording and formatting are never rewritten by a tool.

## Institutional memory

| Document | Path |
| -------- | ---- |
| ADR index | [`docs/adr/README.md`](adr/README.md) |
| ADR-001 … ADR-008 | [`docs/adr/`](adr/) |

## Living documentation (written with the code)

| Document | Audience | Path |
| -------- | -------- | ---- |
| Getting started | Contributors | [`Developer/Getting-Started.md`](Developer/Getting-Started.md) |
| Developer handbook | Contributors | [`Developer/README.md`](Developer/README.md) |
| Testing guide | Contributors | [`Developer/Testing.md`](Developer/Testing.md) |
| Front end | Contributors | [`Developer/Frontend.md`](Developer/Frontend.md) |
| CI and quality gates | Contributors | [`Developer/Continuous-Integration.md`](Developer/Continuous-Integration.md) |
| Repository administration | Maintainers | [`Developer/Repository-Administration.md`](Developer/Repository-Administration.md) |
| Release process | Maintainers | [`Developer/Release-Process.md`](Developer/Release-Process.md) |
| User guide | Guests and hosts | [`User-Guide.md`](User-Guide.md) |
| Administrator guide | Operators | [`Administrator-Guide.md`](Administrator-Guide.md) |
| Troubleshooting | Operators | [`Troubleshooting.md`](Troubleshooting.md) |
| API reference | Integrators | [`api/README.md`](api/README.md) |
| Images | Everyone | [`images/`](images/) |

## Precedence when documents disagree

1. The task or issue you are working on.
2. SAPRS.
3. AI Implementation Guide.
4. ADRs.
5. AI Engineering Playbook.
6. Everything else, including this map (AEP 2).

If you find a genuine conflict, stop and open an issue rather than choosing
silently (AEP 28).
