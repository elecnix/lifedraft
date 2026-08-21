# S04 — Empty the RRSP before 65 to maximize GIS

- **Community source:** PlanEasy "5 Strategies to increase GIS by up to $100,000+"
  <https://www.planeasy.ca/5-strategies-to-help-increase-guaranteed-income-supplement-gis-by-up-to-100000/>;
  Ravi Taxali, "The GIS Optimization Strategy"
  <https://medium.com/the-canadian-investment-retirement-roadmap/the-gis-optimization-strategy-when-it-makes-sense-to-empty-your-rrsp-before-age-65-a892c78be462>.
- **Program / maneuver:** Guaranteed Income Supplement optimization — GIS has one of
  the highest effective clawback rates in Canada (~50-75% on other income).
- **Situation:** A modest-asset retiree deliberately empties the RRSP before 65 (moving
  proceeds into the TFSA) so that from 65 onward taxable income stays low and GIS is
  preserved. If withdrawals are needed later, they are lumped into a single year to
  lose GIS only once.
- **Why it is interesting (complex, counter-intuitive):** For lower-asset households,
  preserving GIS can beat conventional tax-deferral advice; the strategy must be set up
  in the 50s. It is the mirror image of the RRSP meltdown, aimed at a different wealth band.
- **Engine coverage — PARTIAL (Step 1 done; maneuver pending).** GIS itself is
  now PAID by the simulation fold (issue #1020, Step 1): the
  ``retirement_income`` rule calls the existing year-versioned
  ``countries.canada.retirement.gis_benefit`` helper from the PRIOR year's
  GIS-countable income (CRA's prior-year income test — OAS excluded;
  employment income in a still-working prior year IS countable, which is
  why the maneuver must be set up in the 50s), folds the GIS amount into the
  drawdown ``covered_net`` (GIS is non-taxable cash that covers spending,
  reducing the discretionary drawdown shortfall) and into
  ``retirement_income`` / ``total_family_income``, and surfaces it on a new
  ``YearResult.gis_income`` field. The golden household is GIS-ineligible so
  this is a no-op there (terminal ``total_assets`` byte-unchanged). What
  remains is the explicit **GIS-preservation MANEUVER** — the pre-65
  RRSP→TFSA emptying (an accumulation-phase decision with no representation
  in the current search space, which only varies post-65 ``drawdown_order``)
  and the lumped-later-withdrawal timing. That is Step 2 (follow-up).
