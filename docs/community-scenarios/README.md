# Community Scenarios Database (first pass)

A curated database of Canadian personal-finance scenarios drawn from the community
(Reddit r/PersonalFinanceCanada and r/fican, the Financial Wisdom Forum, and
respected Canadian PF blogs) plus official `canada.ca` program pages. Each scenario
features a government program or tax maneuver that a household planning engine could
model. The goal is to feed the roadmap for **lifedraft** and to build a bank of
relatable, marketable situations spanning the *simple* to the *complex*.

Each scenario file follows a fixed schema:

- **Title**
- **Community source (with URL)**
- **Program / maneuver featured**
- **Situation** (2-3 sentences)
- **Why it is interesting** (simple or complex)
- **Engine coverage** — `MODELED`, `PARTIAL`, or `GAP`, with the code path checked

Coverage was verified by grepping the engine under
`countries/canada/` (e.g. `grep -ri fhsa countries/canada/`).

## Index

| # | Scenario | Maneuver | Coverage |
|---|----------|----------|----------|
| [01](s01-smith-manoeuvre.md) | Smith Manoeuvre (readvanceable mortgage) | Interest-deductible investment loan | MODELED |
| [02](s02-fhsa-hbp-first-home.md) | FHSA + Home Buyers' Plan stacking | FHSA, HBP | MODELED |
| [03](s03-rrsp-meltdown.md) | RRSP meltdown before 71 | Early RRIF drawdown | MODELED |
| [04](s04-gis-maximization.md) | Empty the RRSP before 65 for GIS | GIS optimization | PARTIAL |
| [05](s05-oas-clawback-avoidance.md) | OAS clawback avoidance | OAS recovery tax | MODELED |
| [06](s06-lira-lif-unlocking.md) | LIRA/LIF 50% unlocking | Locked-in unlocking | MODELED |
| [07](s07-resp-cesg-catchup.md) | RESP / CESG catch-up | CESG grant | MODELED |
| [08](s08-spousal-rrsp-equalization.md) | Spousal RRSP equalization | Spousal RRSP | MODELED |
| [09](s09-pension-income-splitting.md) | Pension income splitting at 65 | RRIF income splitting | MODELED |
| [10](s10-asset-location-fwt.md) | Asset location & foreign withholding tax | ETF placement | MODELED |
| [11](s11-prescribed-rate-loan.md) | Prescribed-rate spousal loan | Attribution avoidance | MODELED |
| [12](s12-fonds-ftq-lsif.md) | Fonds FTQ / LSIF 30% credit | Labour-sponsored fund | MODELED |
| [13](s13-rdsp-dtc.md) | RDSP + Disability Tax Credit | RDSP grant/bond, DTC | **GAP** |
| [14](s14-corporate-holdco-investing.md) | Corporate / holdco passive investing | Corporate-class funds, SBD | **GAP** |
| [15](s15-ifa-whole-life.md) | Immediate Financing Arrangement | Whole-life leverage | **GAP** |
| [16](s16-capital-gains-harvesting.md) | Capital-gains harvesting & tax-loss selling | Superficial-loss rule | PARTIAL |
| [17](s17-individual-pension-plan.md) | Individual Pension Plan (IPP) | Defined-benefit for owners | **GAP** |
| [18](s18-home-reno-credits.md) | Multigenerational / accessibility reno credits | MHRTC, HATC, caregiver | **GAP** |
| [19](s19-cwb-gst-credit.md) | Canada Workers Benefit & GST/HST credit | Refundable credits | **GAP** |
| [20](s20-cpp-claiming-age.md) | CPP claiming-age timing | Delay CPP to 70 | MODELED |

## Roadmap: what the engine does NOT yet model

The following maneuvers are **GAPs** and are the most valuable output of this pass:

- **RDSP + Disability Tax Credit** (grants/bonds, DTC gating) — see [13](s13-rdsp-dtc.md)
- **Corporate / holdco passive investing** (corporate-class funds, small-business
  deduction grind, CDA, integration) — see [14](s14-corporate-holdco-investing.md)
- **Immediate Financing Arrangement / leveraged whole-life insurance** — see [15](s15-ifa-whole-life.md)
- **Individual Pension Plan (IPP)** for incorporated professionals — see [17](s17-individual-pension-plan.md)
- **Home-renovation credits**: Multigenerational Home Renovation Tax Credit, Home
  Accessibility Tax Credit, Canada Caregiver Credit — see [18](s18-home-reno-credits.md)
- **Canada Workers Benefit and GST/HST credit** (refundable low-income credits and
  their clawbacks) — see [19](s19-cwb-gst-credit.md)

Two more are **PARTIAL** (the calculation exists but the *maneuver* is not optimized):

- **GIS preservation** — GIS is computed, but no strategy deliberately compresses
  RRSP withdrawals to protect GIS — see [04](s04-gis-maximization.md)
- **Tax-loss harvesting / superficial-loss rule** — gains realization and
  bracket-fill drawdown exist, but deliberate loss harvesting and the 30-day
  superficial-loss rule do not — see [16](s16-capital-gains-harvesting.md)
