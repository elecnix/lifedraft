# S10 — Asset location and foreign withholding tax

- **Community source:** Canadian Portfolio Manager Blog, "Foreign Withholding Tax:
  International Equity ETFs"
  <https://canadianportfoliomanagerblog.com/foreign-withholding-tax-international-equity-etfs/>;
  PWL "Asset Location Strategies with the Ludicrous ETF Portfolios"
  <https://benderbenderbortolotti.com/asset-location-strategies-with-the-ludicrous-etf-portfolios/>.
- **Program / maneuver:** Asset location — placing tax-inefficient assets (bonds, REITs,
  foreign dividends) in registered accounts and tax-efficient assets in TFSA/taxable,
  while minimizing Level I/II foreign withholding tax by ETF structure and account type.
- **Situation:** An investor holds Canadian-listed international ETFs (e.g. XEF/VIU) in
  the TFSA/RRSP to shed a layer of U.S. withholding tax, parks bonds in the RRSP, and
  keeps Canadian-dividend and growth equities in taxable to use the dividend credit and
  deferral.
- **Why it is interesting (complex):** The optimum depends jointly on ETF domicile,
  account type, treaty recoverability, and each asset's yield/turnover — a genuine
  multi-account placement problem.
- **Engine coverage — MODELED.** `countries/canada/asset_location.py` models account
  types, ETF/distribution types, withholding tax, and placement
  (tests `tests/test_portfolio_composition.py`).
