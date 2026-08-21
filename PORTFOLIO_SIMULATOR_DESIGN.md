# Open Source Portfolio Simulation Library - Architecture Brainstorm

## Core Design Philosophy

**Separation of Concerns:**
1. **Data Layer** - Tax rules, limits, rates (public, maintained yearly)
2. **Scenario Layer** - Portfolio composition, events, actions
3. **Simulation Layer** - Time stepping, compounding, flows
4. **Optimization Layer** - Scenario comparison, goal-seeking

---

## 1. DATA LAYER (Public, Versioned)

```typescript
// @panoptic/finance-data or @canadian-tax/data
interface TaxParameters {
  year: number;
  federal: TaxBracket[];
  provincial: Record<string, TaxBracket[]>;
}

interface ContributionLimits {
  RRSP: number;     // e.g., 33810 for 2025
  TFSA: number;     // e.g., 7000 for 2025
  CPP: {
    max_pensionable: number;  // e.g., 74600
    rate: 0.0595;
  };
}

interface InvestmentAssumptions {
  expected_return: number;   // e.g., 0.07
  volatility: number;        // e.g., 0.15
  inflation: number;         // e.g., 0.02
}

// Source: PolicyEngine Canada, @equisoft/tax-ca
const CANADA_TAX_2025: TaxParameters = {...};
```

**Key:** Data is VERSIONED separately. Users can compare 2025 vs 2026 vs forecast scenarios.

---

## 2. CORE ENTITIES (Composable)

```typescript
// Asset Types
class Asset {
  id: string;
  type: 'RRSP' | 'TFSA' | 'NonRegistered' | 'RESP' | 'Cash';
  value: number;
  basis: number;  // For ACB tracking
}

class Liability {
  id: string;
  type: 'Mortgage' | 'Loan';
  balance: number;
  rate: number;
}

// Portfolio = Collection of Assets + Liabilities
class Portfolio {
  assets: Asset[];
  liabilities: Liability[];
  cashFlows: CashFlow[];  // Expected inflows/outflows
}

// Time-based Events
class Event {
  year: number;
  month: number;
  type: 'Contribution' | 'Withdrawal' | 'Purchase' | 'Sale' | 'Rebalance';
  amount: number;
  target: string;  // asset ID
  taxTreatment: 'pre-tax' | 'post-tax';
}
```

---

## 3. SCENARIO DEFINITION (Composable DSL)

```typescript
// Base scenario template
const baseScenario = {
  name: "Current Situation",
  startingAge: 35,
  retirementAge: 65,
  portfolio: {
    assets: [
      { id: 'rrsp', type: 'RRSP', value: 100000, basis: 100000 },
      { id: 'tfsa', type: 'TFSA', value: 50000, basis: 50000 }
    ]
  },
  annualIncome: 100000
};

// Alternative scenario - using scenario composition
const extraRevenueScenario = {
  ...baseScenario,
  name: "With Extra $50K Investment",
  oneTimeEvents: [
    {
      year: 2025,
      type: 'Contribution',
      amount: 50000,
      target: 'tfsa',
      taxTreatment: 'post-tax'
    }
  ]
};

// Real estate scenario
const houseDownScenario = {
  ...baseScenario,
  name: "House Down Payment",
  oneTimeEvents: [
    {
      year: 2025,
      type: 'Purchase',
      assetType: 'RealEstate',
      amount: 50000,
      value: 50000
    }
  ],
  liabilities: [
    { id: 'mortgage', type: 'Mortgage', balance: 450000, rate: 0.05 }
  ]
};
```

---

## 4. SIMULATION ENGINE

```typescript
interface SimulationConfig {
  startYear: number;
  endYear: number;
  timeStep: 'monthly' | 'yearly';
  dataSources: TaxParameters[];  // Can mix 2025, 2026, forecast
}

class PortfolioSimulator {
  constructor(private config: SimulationConfig) {}
  
  simulate(portfolio: Portfolio, events: Event[]): SimulationResult {
    let currentPortfolio = {...portfolio};
    const snapshots: PortfolioSnapshot[] = [];
    
    for (let year = this.config.startYear; year <= this.config.endYear; year++) {
      // 1. Apply annual growth
      currentPortfolio = this.applyReturns(currentPortfolio, year);
      
      // 2. Apply events for this year
      currentPortfolio = this.applyEvents(currentPortfolio, events, year);
      
      // 3. Apply tax consequences
      currentPortfolio = this.applyTaxConsequences(currentPortfolio, year);
      
      snapshots.push(this.snapshot(currentPortfolio, year));
    }
    
    return new SimulationResult(snapshots);
  }
}
```

---

## 5. EXTENSIBILITY PATTERNS

### Plugin Architecture for Returns
```typescript
interface ReturnModel {
  calculate(year: number, asset: Asset): number;
}

const BasicReturnModel: ReturnModel = {
  calculate: (year, asset) => 0.07  // 7% nominal
};

const MeanRevertingModel: ReturnModel = {
  calculate: (year, asset) => {
    // More complex model
  }
};
```

### Custom Event Handlers
```typescript
interface EventHandler {
  handle(event: Event, portfolio: Portfolio): Portfolio;
}

const RRSPContributionHandler: EventHandler = {
  handle: (event, portfolio) => {
    // Check contribution room
    // Update basis
    // Apply tax deduction
  }
};
```

---

## 6. OPTIMIZATION FRAMEWORK

```typescript
interface ObjectiveFunction {
  (result: SimulationResult): number;
}

const objectives = {
  maxTerminalValue: (result: SimulationResult) => 
    result.snapshots[result.snapshots.length - 1].totalValue,
  
  maxProbabilitySuccess: (result: SimulationResult) =>
    result.probability(wealth => wealth > 1000000),  // 95% success
  
  minRetirementGap: (result: SimulationResult) =>
    Math.max(0, 1000000 - result.terminalValue())  // Target $1M
};

class Optimizer {
  optimize(
    baseScenario: Scenario,
    alternatives: Scenario[],
    objective: ObjectiveFunction
  ): OptimizationResult {
    return alternatives
      .map(scenario => ({
        scenario,
        score: objective(this.simulate(scenario))
      }))
      .sort((a, b) => b.score - a.score);
  }
}
```

---

## 7. FORECAST DATA INTEGRATION

```typescript
// Users can choose from multiple forecasts
const forecasts = {
  conservative: {
    rrspLimitGrowth: 0.03,
    taxBracketsIndexed: true,
    avgReturn: 0.05
  },
  aggressive: {
    rrspLimitGrowth: 0.05,
    taxBracketsIndexed: true,
    avgReturn: 0.08
  },
  custom: (params: ForecastParams) => {...}
};
```

---

## 8. EXAMPLE USAGE

```typescript
import { 
  Portfolio, Scenario, Simulator, Optimizer,
  CANADA_TAX_2025, FORECAST_CONSERVATIVE 
} from '@open-portfolio/simulator';

// Create scenarios
const scenarios = new ScenarioBuilder()
  .base({ age: 35, portfolio: {...}, income: 100000 })
  .addAlternative('invest-extra', (s) => ({
    ...s,
    events: [...s.events, oneTimeInvestment(50000, 'TFSA')]
  }))
  .addAlternative('buy-house', (s) => ({
    ...s,
    events: [...s.events, housePurchase(50000)],
    liabilities: [mortgage(450000, 0.05)]
  }))
  .build();

// Simulate all
const results = scenarios.map(s => simulator.run(s));

// Optimize
const ranked = optimizer.rank(results, objectives.maxTerminalValue);

console.log("Best scenario:", ranked[0].name);
```

---

## 9. OPEN SOURCE STRUCTURE

```
@open-portfolio/
├── data/           # Tax parameters, contribution limits
├── core/           # Simulation engine, entities
├── plugins/        # Return models, handlers
├── optimizer/      # Scenario comparison tools
└── cli/            # Command-line interface

Examples in separate repo:
@open-portfolio/examples
```

---

## 10. KEY DESIGN PRINCIPLES

1. **Pure Functions** - Same input = same output, testable
2. **Immutable Updates** - Portfolio changes return new objects
3. **Plugin Discovery** - Easy to add new asset types, tax rules
4. **Serialization** - Scenarios can be saved/loaded as JSON
5. **Monte Carlo Ready** - Random seeds for uncertainty
6. **Modular Imports** - Users pick only what they need

This structure allows anyone to:
- Run "what-if" scenarios
- Compare investment alternatives
- Share scenarios as JSON
- Extend with new asset types
- Contribute tax data updates