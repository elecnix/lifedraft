# Open Source Canadian Financial Libraries & Databases for Optimization

## Executive Summary

This document catalogs open source libraries, APIs, and datasets containing Canadian financial intricacies that can be used as input data for optimization algorithms.

---

## 1. Canadian Tax Calculation Libraries

### @equisoft/tax-ca
**Repository:** https://github.com/kronostechnologies/tax-ca  
**NPM:** https://www.npmjs.com/package/@equisoft/tax-ca  
**License:** LGPL-3.0-only  
**Version:** 2026.5.2

#### Features:
- Up-to-date provincial and federal tax data
- Income tax rates and brackets (federal + all provinces)
- CPP/QPP contribution calculations
- EI premium calculations
- Investment account rules (RRSP, TFSA, RRIF limits and withdrawals)
- Pension plan data (CPP, OAS, QPP)

#### Modules Available:
```javascript
import { 
  INVESTMENTS,   // RRSP, TFSA, RRIF limits and rules
  PENSION,       // CPP, OAS, QPP data
  TAXES,         // Federal/provincial income tax, EI, dividend credits
  MISC           // Life expectancy, CPI data
} from '@equisoft/tax-ca';
```

#### Optimization Algorithm Inputs:
- Tax brackets (federal + 13 provinces/territories)
- Marginal tax rates by province/income level
- RRSP contribution limits ($32,840 max for 2025)
- TFSA contribution room tracking (cumulative)
- CPP/QPP contribution ceilings
- Minimum withdrawal calculations for RRIF

---

### @carlo-finance/tax-engine
**Repository:** https://github.com/carlo-finance/tax-engine  
**NPM:** https://www.npmjs.com/package/@carlo-finance/tax-engine  
**License:** MIT  
**Version:** 0.1.59

#### Features:
- Tax calculation engine for personal financial planning
- Designed specifically for optimization use cases

---

### @cantax-fyi/tax-mcp
**NPM:** https://www.npmjs.com/package/@cantax-fyi/tax-mcp  
**License:** MIT  
**Version:** 0.1.18

#### Features:
- MCP (Model Context Protocol) server for Canadian tax
- Integration-ready for AI agents

---

## 2. Financial Modeling & Planning Libraries

### Financial.js / finance.js
**Repository:** https://github.com/nicolaslopezj/finance.js  
**NPM:** https://www.npmjs.com/package/finance  
**License:** MIT

Functions useful for optimization:
- NPV, IRR, payback period calculations
- Amortization schedules
- Bond pricing

---

### Portfolio Optimization Libraries

#### 3.1 PyPortfolioOpt (Python)
**Repository:** https://github.com/robertmartin8/PyPortfolioOpt  
**PyPI:** https://pypi.org/project/pyportfolioopt/  
**License:** MIT

```python
from pypfopt import EfficientFrontier, risk_models, expected_returns
# Can be combined with Canadian asset data
```

#### 3.2 Portfolio-Optim (JavaScript)
**Repository:** https://github.com/melnahas/portfolio-optim  
**NPM:** https://www.npmjs.com/package/portfolio-optim

---

## 3. Canadian Investment Data Sources

### A. ETF Data
Popular Canadian ETFs with available data:

**Broad Market:**
- VCN (FTSE Canada All Cap) - TSX
- VUN (US Total Market) - USD holdings
- VCE/VAB (Canadian bonds)
- VEA/VXUS (International)
- VNQ (REITs)
- VDY (Canadian dividend)

### B. Bank Rate Data
API sources for Canadian interest rates:
- Bank of Canada rates API
- CDIC coverage limits (static data)

---

## 4. Government Open Data

### A. Bank of Canada API
**URL:** https://www.bankofcanada.ca/valet/  
**Documentation:** https://www.bankofcanada.ca/valet/docs/

Available data:
- Policy interest rate
- Inflation data (CPI)
- Exchange rates
- Bond yields

### B. Statistics Canada API
**URL:** https://www.statcan.gc.ca/eng/developers  
**API:** https://api.statcan.gc.ca/portal/en

Data series include:
- CPI by city/category
- Income statistics
- Housing price indexes

---

## 5. Complete Data Structure for Optimization

### Tax Parameters Matrix:
```json
{
  "federal": {
    "taxBrackets": [[53359, 0.15], [106717, 0.205], [165430, 0.26], [inf, 0.29]],
    "cppMaxPensionable": 66600,
    "cppRate": 0.0595,
    "eiMaxInsurable": 61400,
    "eiRate": 0.0163
  },
  "province": {
    "ON": { "taxBrackets": [...] },
    "BC": { "taxBrackets": [...] },
    "AB": { "taxBrackets": [...] },
    ...
  }
}
```

### Account Rules:
```json
{
  "RRSP": {
    "annualLimit": 32840,
    "withdrawalTaxedAsIncome": true,
    "withholdingRates": [0.1, 0.2, 0.3]
  },
  "TFSA": {
    "annualLimit": 7000,
    "cumulativeRoom": true,
    "withdrawalTaxFree": true
  },
  "RRIF": {
    "minWithdrawalRate": 0.052,
    "ageFactor": 0.01
  }
}
```

---

## 6. Integration Example

### Basic Canadian Tax Calculator:
```javascript
import { TAXES, INVESTMENTS } from '@equisoft/tax-ca';

function calculateAfterTaxIncome(brackets, income) {
  let tax = 0;
  let remaining = income;
  
  for (let [threshold, rate] of brackets) {
    const taxableInBracket = Math.min(remaining, threshold);
    tax += taxableInBracket * rate;
    remaining -= taxableInBracket;
    if (remaining <= 0) break;
  }
  
  return income - tax;
}

// Optimize RRSP contribution
function optimizeRRSP(income, taxBrackets) {
  const maxRRSP = Math.min(INVESTMENTS.RegisteredRetirementSavingsPlan.LIMIT, income * 0.18);
  const marginalRate = getCurrentMarginalRate(taxBrackets, income);
  return { maxRRSP, taxSavings: maxRRSP * marginalRate };
}
```

---

## 7. Recommended Stack for Financial Optimization

### Data Layer:
1. **@equisoft/tax-ca** - Core tax data
2. **Bank of Canada API** - Interest rate environment
3. **Static ETF data** - Asset allocation options

### Optimization Layer:
1. **Custom algorithm** using tax data
2. **Linear programming** for asset allocation
3. **Monte Carlo simulation** for scenario testing

### API Layer:
1. **@cantax-fyi/tax-mcp** - MCP integration
2. **Custom GraphQL** wrapping tax and investment data

---

## 8. Additional Resources

### Documentation:
- CRA Guide for Financial Planners: https://www.canada.ca/en/revenue-agency/services/e-services/e-services-individuals/account-individuals.html
- Provincial tax guides (each province website)
- IIROC rules for investment products

### GitHub Topics to Explore:
- `canadian-tax`
- `personal-finance`
- `financial-planning`
- `retirement-calculators`
- `tax-optimization`

---

## 9. Data Freshness Considerations

- Tax data updates annually (April)
- Bank of Canada rates update after each policy meeting
- TFSA/RRSP limits change yearly
- Recommended: Cache data with TTL of 7-30 days

---

*Generated: May 2026*