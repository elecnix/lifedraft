#!/usr/bin/env python3
"""
Return Model — Pluggable investment return models (DP#21).

Per DP#8: ReturnModel is a single dataclass with a type field.
ReturnEngine is a stateless dispatch engine that computes returns
from the data. This makes return models serializable, comparable,
and composable without subclass coupling.

Models:
    fixed: Constant rate every year
    variable: Explicit sequence of per-year rates
    stochastic: Random returns from a distribution with reproducible seeds (DP#23)
    mean_reverting: Ornstein-Uhlenbeck mean-reverting process
    stressed: Market crash followed by recovery

Usage:
    from return_model import ReturnModel, ReturnEngine

    # Simple: 7% fixed
    model = ReturnModel(type="fixed", rate=0.07)
    ret = ReturnEngine.return_for_year(model, year=0)

    # Monte Carlo: lognormal with seed
    model = ReturnModel(type="stochastic", mean=0.07, sigma=0.15, seed=42)
    ret = ReturnEngine.return_for_year(model, year=0)

Convenience factories:
    FixedReturn(rate) → ReturnModel(type="fixed", rate=rate)
    StochasticReturn(mean, sigma, seed) → ReturnModel(type="stochastic", ...)
    build_return_model(...) → same as before, returns ReturnModel
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union
import math


@dataclass
class ReturnModel:
    """Investment return model data (DP#8: data, not inheritance).

    All return model types are represented by a single dataclass.
    The `type` field determines which dispatch path the engine uses.
    Unused fields for a given type are ignored.

    Fields are grouped by model type for clarity, but all are optional
    so that any model type can be constructed with just the relevant ones.
    """
    type: str = "fixed"  # "fixed", "variable", "stochastic", "mean_reverting", "stressed"
    name: str = ""

    # fixed / variable fallback / stochastic beyond n_years / stressed baseline
    rate: float = 0.07

    # variable
    rates: List[float] = field(default_factory=list)
    fallback: float = 0.07

    # stochastic / mean_reverting
    mean: float = 0.07
    sigma: float = 0.15
    seed: int = 42
    n_years: int = 50

    # mean_reverting
    long_term_mean: float = 0.07
    reversion_speed: float = 0.3
    volatility: float = 0.02
    initial: float = 0.07

    # stressed
    crash_year: int = 2
    crash_pct: float = -0.40
    recovery_years: int = 5

    # internal: pre-generated rates for stochastic/mean_reverting
    _generated_rates: Optional[List[float]] = field(default=None, repr=False)

    def __post_init__(self):
        if not self.name:
            self.name = self.type

        if self.type == "stochastic":
            self._generated_rates = _generate_stochastic_rates(
                mean=self.mean, sigma=self.sigma, seed=self.seed, n_years=self.n_years
            )
        elif self.type == "mean_reverting":
            self._generated_rates = _generate_mean_reverting_rates(
                long_term_mean=self.long_term_mean,
                reversion_speed=self.reversion_speed,
                volatility=self.volatility,
                initial=self.initial,
                seed=self.seed,
                n_years=self.n_years,
            )

    def return_for_year(self, year: int) -> float:
        """Convenience: delegates to ReturnEngine."""
        return ReturnEngine.return_for_year(self, year)

    def to_dict(self) -> dict:
        """Serialize to dict. DP#24: round-trip."""
        d = {"type": self.type}
        if self.type == "fixed":
            d["rate"] = self.rate
        elif self.type == "variable":
            d["rates"] = self.rates
            d["fallback"] = self.fallback
        elif self.type == "stochastic":
            d["mean"] = self.mean
            d["sigma"] = self.sigma
            d["seed"] = self.seed
            d["n_years"] = self.n_years
        elif self.type == "mean_reverting":
            d.update({
                "long_term_mean": self.long_term_mean,
                "reversion_speed": self.reversion_speed,
                "volatility": self.volatility,
                "initial": self.initial,
                "seed": self.seed,
                "n_years": self.n_years,
            })
        elif self.type == "stressed":
            d.update({
                "baseline_rate": self.rate,
                "crash_year": self.crash_year,
                "crash_pct": self.crash_pct,
                "recovery_years": self.recovery_years,
            })
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'ReturnModel':
        """Deserialize from dict. DP#24: round-trip."""
        rtype = data.get("type", "fixed")
        if rtype == "fixed":
            return cls(type="fixed", rate=data.get("rate", 0.07))
        elif rtype == "variable":
            return cls(type="variable", rates=data.get("rates", []),
                        fallback=data.get("fallback", data.get("rate", 0.07)))
        elif rtype == "stochastic":
            return cls(type="stochastic",
                        mean=data.get("mean", 0.07),
                        sigma=data.get("sigma", 0.15),
                        seed=data.get("seed", 42),
                        n_years=data.get("n_years", 50))
        elif rtype == "mean_reverting":
            return cls(type="mean_reverting",
                        long_term_mean=data.get("long_term_mean", data.get("mean", 0.07)),
                        reversion_speed=data.get("reversion_speed", 0.3),
                        volatility=data.get("volatility", 0.02),
                        initial=data.get("initial", data.get("rate", 0.07)),
                        seed=data.get("seed", 42),
                        n_years=data.get("n_years", 50))
        elif rtype == "stressed":
            return cls(type="stressed",
                        rate=data.get("baseline_rate", data.get("rate", 0.07)),
                        crash_year=data.get("crash_year", 2),
                        crash_pct=data.get("crash_pct", -0.40),
                        recovery_years=data.get("recovery_years", 5))
        else:
            raise ValueError(f"Unknown return model type: {rtype}")


def _generate_stochastic_rates(mean: float, sigma: float, seed: int,
                                n_years: int) -> List[float]:
    """Pre-generate stochastic return sequence for reproducibility (DP#23)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    raw = rng.normal(loc=mean, scale=sigma, size=n_years)
    clipped = np.clip(raw, -0.30, 0.50)
    return [float(r) for r in clipped]


def _generate_mean_reverting_rates(long_term_mean: float, reversion_speed: float,
                                    volatility: float, initial: float,
                                    seed: int, n_years: int) -> List[float]:
    """Pre-generate mean-reverting return sequence for reproducibility (DP#23)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    rates = [initial]
    for t in range(1, n_years):
        deviation = rates[-1] - long_term_mean
        shock = rng.normal(0, volatility)
        new_rate = rates[-1] - reversion_speed * deviation + shock
        rates.append(max(-0.15, min(0.40, new_rate)))
    return rates


class ReturnEngine:
    """Stateless dispatch engine for return models (DP#8).

    Pure functions that take (model, year) and return the rate.
    No state, no side effects. Same inputs → same outputs.
    """

    @staticmethod
    def return_for_year(model: ReturnModel, year: int) -> float:
        """Return the investment return rate for a given year index."""
        if model.type == "fixed":
            return model.rate
        elif model.type == "variable":
            if year < len(model.rates):
                return model.rates[year]
            return model.fallback
        elif model.type == "stochastic":
            if model._generated_rates is not None and year < len(model._generated_rates):
                return model._generated_rates[year]
            return model.mean
        elif model.type == "mean_reverting":
            if model._generated_rates is not None and year < len(model._generated_rates):
                return model._generated_rates[year]
            return model.long_term_mean
        elif model.type == "stressed":
            return _stressed_return(
                year, model.rate, model.crash_year,
                model.crash_pct, model.recovery_years
            )
        else:
            raise ValueError(f"Unknown return model type: {model.type}")


def _stressed_return(year: int, baseline_rate: float, crash_year: int,
                      crash_pct: float, recovery_years: int) -> float:
    """Compute stressed return for a single year."""
    if year == crash_year:
        return crash_pct
    elif year > crash_year and year <= crash_year + recovery_years:
        recovery_progress = (year - crash_year) / recovery_years
        recovery_rate = baseline_rate + (1 - recovery_progress) * 0.05
        return min(recovery_rate, baseline_rate * 1.2)
    else:
        return baseline_rate


# ─── Return model factory functions ───

def FixedReturn(rate: float = 0.07) -> ReturnModel:
    """Create a fixed return model."""
    return ReturnModel(type="fixed", rate=rate, name="fixed")


def VariableReturn(rates: List[float] = None, fallback: float = 0.07) -> ReturnModel:
    """Create a variable return model."""
    return ReturnModel(type="variable", rates=rates or [], fallback=fallback, name="variable")


def StochasticReturn(mean: float = 0.07, sigma: float = 0.15,
                      seed: int = 42, n_years: int = 50) -> ReturnModel:
    """Create a stochastic return model."""
    return ReturnModel(type="stochastic", mean=mean, sigma=sigma,
                        seed=seed, n_years=n_years, name="stochastic")


def MeanRevertingReturn(long_term_mean: float = 0.07, reversion_speed: float = 0.3,
                         volatility: float = 0.02, initial: float = 0.07,
                         seed: int = 42, n_years: int = 50) -> ReturnModel:
    """Create a mean-reverting return model."""
    return ReturnModel(type="mean_reverting", long_term_mean=long_term_mean,
                        reversion_speed=reversion_speed, volatility=volatility,
                        initial=initial, seed=seed, n_years=n_years, name="mean_reverting")


def StressedReturn(baseline_rate: float = 0.07, crash_year: int = 2,
                    crash_pct: float = -0.40, recovery_years: int = 5) -> ReturnModel:
    """Create a stressed return model."""
    return ReturnModel(type="stressed", rate=baseline_rate,
                        crash_year=crash_year, crash_pct=crash_pct,
                        recovery_years=recovery_years, name="stressed")


# ─── DEAD CODE REMOVED (epic #603 Track C Phase 2, DP#9) ───
#
# RateScenarioPath / RateScenarioConfig used to live here, parsing the
# rate_scenarios.scenarios[] input block. Neither class had a production
# caller -- RateScenarioConfig.from_dict was exercised only by tests, and
# SimulationConfig.rate_scenario_data (the field it fed) was read nowhere
# outside the loader and tests (#593, DEAD_ALLOWLIST). Deleted rather than
# kept as an unwired feature (DP#9: a feature that has never run is not a
# feature, it's a liability).


def build_return_model(return_type: str = "fixed",
                       rate: float = 0.07,
                       rates_list: List[float] = None,
                       mean: float = 0.07,
                       sigma: float = 0.15,
                       seed: int = 42,
                       n_years: int = 50,
                       stressed: dict = None,
                       mean_reverting: dict = None) -> ReturnModel:
    """Factory function to build a ReturnModel from parameters.

    Mirrors build_rate_path() from rate_model.py.

    Args:
        return_type: 'fixed', 'variable', 'stochastic', 'mean_reverting', or 'stressed'
        rate: Fixed rate (for 'fixed' type)
        rates_list: Per-year rates (for 'variable' type)
        mean: Expected return (for 'stochastic' and 'mean_reverting')
        sigma: Standard deviation (for 'stochastic')
        seed: Random seed (DP#23)
        n_years: Number of years to generate
        stressed: Dict with crash_year, crash_pct, recovery_years (for 'stressed')
        mean_reverting: Dict with long_term_mean, reversion_speed, volatility, initial

    Returns:
        ReturnModel instance
    """
    if return_type == "fixed":
        return ReturnModel(type="fixed", rate=rate)
    elif return_type == "variable":
        return ReturnModel(type="variable", rates=rates_list or [], fallback=rate)
    elif return_type == "stochastic":
        return ReturnModel(type="stochastic", mean=mean, sigma=sigma,
                            seed=seed, n_years=n_years)
    elif return_type == "mean_reverting":
        params = mean_reverting or {}
        return ReturnModel(
            type="mean_reverting",
            long_term_mean=params.get('long_term_mean', mean),
            reversion_speed=params.get('reversion_speed', 0.3),
            volatility=params.get('volatility', 0.02),
            initial=params.get('initial', rate),
            seed=params.get('seed', seed),
            n_years=n_years,
        )
    elif return_type == "stressed":
        params = stressed or {}
        return ReturnModel(
            type="stressed",
            rate=params.get('baseline_rate', rate),
            crash_year=params.get('crash_year', 2),
            crash_pct=params.get('crash_pct', -0.40),
            recovery_years=params.get('recovery_years', 5),
        )
    else:
        raise ValueError(f"Unknown return_type: {return_type}")


def build_return_model_from_config(config: dict) -> ReturnModel:
    """Build a ReturnModel from input.json return_model section.

    Per DP#14: scripts read a common config schema.
    Supports all return model types plus the new stressed and mean_reverting types.

    Args:
        config: The 'return_model' section from input.json

    Returns:
        ReturnModel instance
    """
    rtype = config.get('type', 'fixed')

    if rtype == 'fixed':
        return ReturnModel(type="fixed", rate=config.get('rate', 0.07))
    elif rtype == 'variable':
        return ReturnModel(
            type="variable",
            rates=config.get('rates', []),
            fallback=config.get('fallback', config.get('rate', 0.07))
        )
    elif rtype == 'stochastic':
        return ReturnModel(
            type="stochastic",
            mean=config.get('mean', 0.07),
            sigma=config.get('sigma', 0.15),
            seed=config.get('seed', 42),
            n_years=config.get('n_years', 50),
        )
    elif rtype == 'mean_reverting':
        params = config.get('mean_reverting', {})
        return ReturnModel(
            type="mean_reverting",
            long_term_mean=params.get('long_term_mean', config.get('mean', 0.07)),
            reversion_speed=params.get('reversion_speed', 0.3),
            volatility=params.get('volatility', 0.02),
            initial=params.get('initial', config.get('rate', 0.07)),
            seed=params.get('seed', 42),
            n_years=config.get('n_years', 50),
        )
    elif rtype == 'stressed':
        params = config.get('stressed', {})
        return ReturnModel(
            type="stressed",
            rate=params.get('baseline_rate', config.get('rate', 0.07)),
            crash_year=params.get('crash_year', 2),
            crash_pct=params.get('crash_pct', -0.40),
            recovery_years=params.get('recovery_years', 5),
        )
    else:
        raise ValueError(f"Unknown return_model type: {rtype}")