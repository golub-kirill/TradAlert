"""
backtest/multiple_testing.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Multiple-testing / data-snooping corrections for the TradAlert backtests.

When a strategy's parameters are chosen by trying many configurations and
keeping the best, the winner's in-sample Sharpe is inflated: the maximum of N
noisy estimates is biased upward even when no real edge exists. These functions
quantify and discount that bias.

Two complementary corrections:

  • Deflated Sharpe Ratio (DSR) — Bailey & López de Prado (2014). The probability
    that the selected strategy's *true* Sharpe exceeds the expected MAXIMUM
    Sharpe attainable by chance across the N trials searched. DSR > 0.95 ≈ the
    edge survives the search.

  • White's Reality Check (RC) — White (2000), with the Politis–Romano (1994)
    stationary bootstrap. Tests H0: "the best of the N strategies has no edge
    over the benchmark", accounting for the snooping of having picked the best.
    A small p-value ≈ the best strategy's outperformance is real.

Like ``stats_utils``, this module depends only on ``math`` + ``numpy`` (no
scipy, no backtest infrastructure) so it imports anywhere and is unit-testable
on synthetic arrays.

Convention note
───────────────
Every Sharpe passed to the DSR/PSR machinery must be **per-period**
(monthly, NON-annualised) — ``mean / std`` of the monthly-R series, *not* the
×√12 figure reported elsewhere. SR_hat uses the sample std (ddof=1) to match
``stats_utils.sharpe_ratio``; skew / kurtosis are the plug-in (population)
standardized moments. Kurtosis is **non-excess** (normal = 3.0).

Public API
──────────
    norm_cdf(x)                                          → float   Φ(x)
    norm_ppf(p)                                          → float   Φ⁻¹(p)
    probabilistic_sharpe_ratio(monthly_r, sr_benchmark) → PSRResult
    expected_max_sharpe(sr_variance, n_trials)          → float    SR0
    deflated_sharpe_ratio(selected_monthly_r,
                          all_monthly_sharpes, n_trials) → DSRResult
    align_monthly_matrix(series_by_config)              → (np.ndarray, list[str])
    whites_reality_check(monthly_matrix, ...)           → RealityCheckResult
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

# Euler–Mascheroni constant — drives the expected-maximum order statistic.
_EULER_MASCHERONI = 0.5772156649015329


# ── normal CDF / inverse CDF (scipy-free) ───────────────────────────────────────

def norm_cdf(x: float) -> float:
    """Standard normal CDF Φ(x) via the error function: 0.5·(1 + erf(x/√2))."""
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


# Acklam's rational approximation to the inverse normal CDF.
_ACKLAM_A = (
    -3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
    1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00,
)
_ACKLAM_B = (
    -5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
    6.680131188771972e+01, -1.328068155288572e+01,
)
_ACKLAM_C = (
    -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
    -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00,
)
_ACKLAM_D = (
    7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
    3.754408661907416e+00,
)
_ACKLAM_P_LOW = 0.02425


def norm_ppf(p: float) -> float:
    """
    Inverse standard normal CDF Φ⁻¹(p) via Acklam's rational approximation,
    refined with one Halley step for ~machine precision.

    Raises ValueError for p ≤ 0 or p ≥ 1.
    """
    p = float(p)
    if not (0.0 < p < 1.0):
        raise ValueError(f"norm_ppf domain is (0, 1); got {p}")

    a, b, c, d = _ACKLAM_A, _ACKLAM_B, _ACKLAM_C, _ACKLAM_D
    p_low, p_high = _ACKLAM_P_LOW, 1.0 - _ACKLAM_P_LOW

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) \
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q \
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) \
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)

    # One Halley refinement step.
    e = norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    x = x - u / (1.0 + x * u / 2.0)
    return float(x)


# ── standardized moments (plug-in / population) ─────────────────────────────────

def _skew(x: np.ndarray) -> float:
    """Population skewness γ3 = m3 / m2**1.5 (0.0 if undefined)."""
    arr = np.asarray(x, dtype=float)
    if len(arr) < 3:
        return 0.0
    d = arr - arr.mean()
    m2 = float((d ** 2).mean())
    if m2 <= 0.0:
        return 0.0
    m3 = float((d ** 3).mean())
    return m3 / m2 ** 1.5


def _kurtosis(x: np.ndarray) -> float:
    """Population NON-excess kurtosis γ4 = m4 / m2**2 (normal = 3.0)."""
    arr = np.asarray(x, dtype=float)
    if len(arr) < 4:
        return 3.0
    d = arr - arr.mean()
    m2 = float((d ** 2).mean())
    if m2 <= 0.0:
        return 3.0
    m4 = float((d ** 4).mean())
    return m4 / m2 ** 2


# ── Probabilistic Sharpe Ratio ──────────────────────────────────────────────────

@dataclass(frozen=True)
class PSRResult:
    """
    Probabilistic Sharpe Ratio — probability the true Sharpe exceeds a benchmark.

    Attributes
    ----------
    psr          : Φ[...] probability that true SR > ``sr_benchmark``.
    sr_hat       : Observed per-period (monthly) Sharpe, std with ddof=1.
    sr_benchmark : The SR* the estimate was tested against.
    n_periods    : Number of periods (months) T.
    skew         : γ3 of the return series.
    kurtosis     : γ4 (non-excess; normal = 3) of the return series.
    """
    psr: float
    sr_hat: float
    sr_benchmark: float
    n_periods: int
    skew: float
    kurtosis: float

    def __str__(self) -> str:
        return (f"PSR={self.psr:.3f}  SR_hat={self.sr_hat:+.3f}  "
                f"vs SR*={self.sr_benchmark:.3f}  T={self.n_periods}")


def probabilistic_sharpe_ratio(
        monthly_r: Sequence[float],
        sr_benchmark: float = 0.0,
) -> PSRResult:
    """
    PSR(SR*) per Bailey & López de Prado (2014):

        PSR = Φ[ (SR_hat − SR*) · √(T−1)
                 / √(1 − γ3·SR_hat + ((γ4−1)/4)·SR_hat²) ]

    where SR_hat is the per-period (monthly, NON-annualised) Sharpe, T the
    number of months, γ3 the skew and γ4 the non-excess kurtosis — all derived
    from the SAME ``monthly_r`` series.

    Returns psr = NaN when T < 2, the std is 0, or the variance term is
    non-positive (a degenerate higher-moment combination).
    """
    arr = np.asarray(monthly_r, dtype=float)
    T = len(arr)
    sr_b = float(sr_benchmark)
    if T < 2:
        return PSRResult(float("nan"), float("nan"), sr_b, T, float("nan"), float("nan"))
    std = arr.std(ddof=1)
    if std == 0:
        return PSRResult(float("nan"), float("nan"), sr_b, T, float("nan"), float("nan"))

    sr_hat = float(arr.mean() / std)
    g3 = _skew(arr)
    g4 = _kurtosis(arr)
    denom = 1.0 - g3 * sr_hat + ((g4 - 1.0) / 4.0) * sr_hat ** 2
    if denom <= 0.0:
        return PSRResult(float("nan"), sr_hat, sr_b, T, g3, g4)

    z = (sr_hat - sr_b) * math.sqrt(T - 1) / math.sqrt(denom)
    return PSRResult(norm_cdf(z), sr_hat, sr_b, T, g3, g4)


# ── Expected maximum Sharpe under the null (SR0) ────────────────────────────────

def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """
    Expected maximum of N independent zero-skill Sharpe estimates
    (Bailey & López de Prado 2014):

        SR0 = √Var({SR_n}) · [ (1−γ)·Φ⁻¹(1 − 1/N)
                               + γ·Φ⁻¹(1 − 1/(N·e)) ]

    γ = Euler–Mascheroni ≈ 0.5772.  ``sr_variance`` is the cross-sectional
    sample variance (ddof=1) of the per-period Sharpes across the N trials.

    Returns 0.0 for N < 2 or sr_variance ≤ 0 (no search inflation to correct).
    """
    if n_trials < 2 or sr_variance <= 0.0:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(sr_variance) *
                 ((1.0 - _EULER_MASCHERONI) * z1 + _EULER_MASCHERONI * z2))


# ── Deflated Sharpe Ratio ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class DSRResult:
    """
    Deflated Sharpe Ratio = PSR evaluated against the expected-max benchmark SR0.

    Attributes
    ----------
    dsr          : PSR(SR0) — probability the selected SR beats the chance maximum.
    psr_vs_zero  : PSR(0) for context (the un-deflated probability).
    sr0          : Expected-max Sharpe hurdle (per-period) from N trials.
    sr_hat       : Selected config's per-period Sharpe.
    n_trials     : N configs entering the deflation.
    n_periods    : Months T in the selected config's series.
    """
    dsr: float
    psr_vs_zero: float
    sr0: float
    sr_hat: float
    n_trials: int
    n_periods: int

    def __str__(self) -> str:
        return (f"DSR={self.dsr:.3f}  (PSR(0)={self.psr_vs_zero:.3f}, "
                f"SR0={self.sr0:.3f}, N={self.n_trials}, T={self.n_periods})")


def deflated_sharpe_ratio(
        selected_monthly_r: Sequence[float],
        all_monthly_sharpes: Sequence[float],
        n_trials: int | None = None,
) -> DSRResult:
    """
    Deflated Sharpe Ratio of the selected (winning) config.

    Parameters
    ----------
    selected_monthly_r  : Monthly-R series of the winning config (per-period).
    all_monthly_sharpes : Per-period (NON-annualised) Sharpe of EVERY trial,
                          including the winner — used for Var({SR_n}) and, by
                          default, N. Non-finite entries are dropped.
    n_trials            : Override N; defaults to the count of finite Sharpes.

    DSR ≤ PSR(0) always (SR0 ≥ 0 raises the hurdle). When N < 2 or the trial
    Sharpes have zero variance, SR0 = 0 and DSR == PSR(0).
    """
    clean = np.asarray(
        [s for s in all_monthly_sharpes if s is not None and math.isfinite(float(s))],
        dtype=float,
    )
    n = int(n_trials) if n_trials is not None else len(clean)
    var = float(np.var(clean, ddof=1)) if len(clean) >= 2 else 0.0
    sr0 = expected_max_sharpe(var, n)

    psr_def = probabilistic_sharpe_ratio(selected_monthly_r, sr_benchmark=sr0)
    psr_zero = probabilistic_sharpe_ratio(selected_monthly_r, sr_benchmark=0.0)

    return DSRResult(
        dsr=psr_def.psr,
        psr_vs_zero=psr_zero.psr,
        sr0=sr0,
        sr_hat=psr_def.sr_hat,
        n_trials=n,
        n_periods=psr_def.n_periods,
    )


# ── monthly-R alignment ─────────────────────────────────────────────────────────

def align_monthly_matrix(
        series_by_config: Sequence[Mapping[str, float]],
) -> tuple[np.ndarray, list[str]]:
    """
    Build the (T × K) aligned monthly-R matrix from K per-config monthly series.

    Each element is a mapping "YYYY-MM" → R (a pandas Series with a month index
    works — anything with ``.to_dict()`` or that ``dict()`` accepts). Rows are
    the sorted UNION of all month keys; a month a config did not trade is 0.0 R
    (it deployed no risk that month). Returns ``(matrix, month_keys)``.

    An empty input (or all-empty series) returns a (0, 0) array and [].
    """
    dicts: list[dict[str, float]] = []
    months: set[str] = set()
    for s in series_by_config:
        if hasattr(s, "to_dict"):
            d = {str(k): float(v) for k, v in s.to_dict().items()}
        else:
            d = {str(k): float(v) for k, v in dict(s).items()}
        dicts.append(d)
        months.update(d.keys())

    month_keys = sorted(months)
    if not month_keys or not dicts:
        return np.zeros((0, 0), dtype=float), []

    row_of = {m: i for i, m in enumerate(month_keys)}
    mat = np.zeros((len(month_keys), len(dicts)), dtype=float)
    for j, d in enumerate(dicts):
        for m, v in d.items():
            mat[row_of[m], j] = v
    return mat, month_keys


# ── White's Reality Check (stationary bootstrap) ────────────────────────────────

def _stationary_bootstrap_indices(
        n: int,
        mean_block: float,
        size: int,
        rng: np.random.Generator,
) -> np.ndarray:
    """
    Politis–Romano (1994) stationary-bootstrap index sequence over 0..n-1.

    Block lengths are geometric with restart probability p = 1/mean_block;
    runs wrap circularly. The SAME index array is applied to every column so
    contemporaneous cross-config correlation is preserved.
    """
    if n <= 0 or size <= 0:
        return np.zeros(0, dtype=int)
    p = 1.0 / mean_block if mean_block > 0 else 1.0
    idx = np.empty(size, dtype=int)
    cur = int(rng.integers(0, n))
    for t in range(size):
        if t == 0 or rng.random() < p:
            cur = int(rng.integers(0, n))
        else:
            cur = (cur + 1) % n
        idx[t] = cur
    return idx


@dataclass(frozen=True)
class RealityCheckResult:
    """
    White's Reality Check output.

    Attributes
    ----------
    p_value         : RC p-value. Low ⇒ the best config's edge survives snooping.
    best_config_idx : Column index achieving the observed maximum (−1 if N/A).
    observed_stat   : V = max_k √T · f̄_k.
    n_bootstrap     : Number of bootstrap resamples B.
    n_configs       : Columns K in the matrix.
    n_periods       : Rows T in the matrix.
    mean_block      : Mean bootstrap block length used.
    """
    p_value: float
    best_config_idx: int
    observed_stat: float
    n_bootstrap: int
    n_configs: int
    n_periods: int
    mean_block: float

    def __str__(self) -> str:
        return (f"White RC p={self.p_value:.4f}  (V={self.observed_stat:.3f}, "
                f"best=col{self.best_config_idx}, K={self.n_configs}, "
                f"T={self.n_periods}, B={self.n_bootstrap})")


def whites_reality_check(
        monthly_matrix: np.ndarray,
        n_bootstrap: int = 5000,
        mean_block: float = 6.0,
        seed: int = 42,
) -> RealityCheckResult:
    """
    White's (2000) Reality Check for data snooping, benchmark = 0 (cash / no
    edge), stationary-bootstrap variant.

        f̄_k = mean monthly R of config k                  (performance vs 0)
        V    = max_k √T · f̄_k                              (observed statistic)
        for b in 1..B:
            resample one month-index sequence (stationary bootstrap),
            apply to ALL columns,
            V*_b = max_k √T · (f̄*_k − f̄_k)                (recentred on f̄)
        p = (1 + #{ V*_b ≥ V }) / (B + 1)

    A small p-value means the best config beats cash by more than data-snooping
    over K candidates can explain. Seeded for reproducibility.

    Returns p_value = NaN when the matrix has < 2 rows or < 1 column.
    """
    mat = np.asarray(monthly_matrix, dtype=float)
    if mat.ndim != 2 or mat.shape[0] < 2 or mat.shape[1] < 1:
        return RealityCheckResult(
            p_value=float("nan"), best_config_idx=-1, observed_stat=float("nan"),
            n_bootstrap=0,
            n_configs=int(mat.shape[1]) if mat.ndim == 2 else 0,
            n_periods=int(mat.shape[0]) if mat.ndim == 2 else 0,
            mean_block=mean_block,
        )

    T, K = mat.shape
    sqrt_t = math.sqrt(T)
    fbar = mat.mean(axis=0)
    observed = sqrt_t * fbar
    V = float(observed.max())
    best = int(observed.argmax())

    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(n_bootstrap):
        idx = _stationary_bootstrap_indices(T, mean_block, T, rng)
        fbar_star = mat[idx, :].mean(axis=0)
        v_b = float((sqrt_t * (fbar_star - fbar)).max())
        if v_b >= V:
            count += 1

    p_value = (1.0 + count) / (n_bootstrap + 1.0)
    return RealityCheckResult(
        p_value=p_value, best_config_idx=best, observed_stat=V,
        n_bootstrap=n_bootstrap, n_configs=K, n_periods=T, mean_block=mean_block,
    )
