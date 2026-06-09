"""
Bootstrap report display fixes (audit E1/E3):
  - the "significant" (CI-excludes-0) mark is only meaningful for signed metrics
    (expectancy, total_r); win_rate and profit_factor are always > 0;
  - the resample count in the header is derived from the data, not hard-coded.
"""

from __future__ import annotations

from backtest.report import print_bootstrap
from backtest.stats_utils import BootstrapResult


def test_significance_only_on_signed_metrics_and_derived_n(capsys):
    boot = {
        "expectancy": BootstrapResult(0.10, 0.05, 0.15, 0.95, 5000, 0.02),
        "win_rate": BootstrapResult(0.45, 0.40, 0.50, 0.95, 5000, 0.01),
        "total_r": BootstrapResult(100.0, 50.0, 150.0, 0.95, 5000, 30.0),
        "profit_factor": BootstrapResult(1.30, 1.10, 1.50, 0.95, 5000, 0.08),
    }
    print_bootstrap(boot)
    out = capsys.readouterr().out

    # Header reflects the actual resample count, not a hard-coded 10 000.
    assert "n=5,000 resamples" in out

    # win_rate / profit_factor rows must carry no significance mark.
    for line in out.splitlines():
        if "win_rate" in line or "profit_factor" in line:
            assert "✓" not in line and "—" not in line
        if line.strip().startswith("expectancy"):
            assert "✓" in line  # signed + CI excludes 0 -> significant
