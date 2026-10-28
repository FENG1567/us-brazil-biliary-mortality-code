"""Check published revision arithmetic without claiming to recreate its inputs."""
from pathlib import Path
import argparse
import csv
import json
import math

def rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))

def verify(root):
    if not __debug__:
        raise RuntimeError('Do not run validation with python -O or PYTHONOPTIMIZE')
    data = root/'revision/results'
    rates = rows(data/'Revision_all_age_and_0_74_bounds.csv')
    for r in rates:
        base = float(r['complete_case_rate_per_100000'])
        assert math.isclose(base+float(r['missing_allocation_min_increment']), float(r['rate_lower']), abs_tol=1e-12)
        assert math.isclose(base+float(r['missing_allocation_max_increment']), float(r['rate_upper']), abs_tol=1e-12)
        assert float(r['rate_lower']) <= float(r['rate_upper'])
    effects = rows(data/'Revision_effect_bounds.csv')
    for r in effects:
        assert float(r['lower_bound']) <= float(r['upper_bound'])
        assert 'not a confidence interval' in r['bound_type']
    checks = rows(data/'Revision_population_baseline_replication_checks.csv')
    assert len(checks) == 60
    assert max(float(r['absolute_difference']) for r in checks) <= 1e-12
    intervals = rows(data/'Revision_population_temporal_contrast_intervals.csv')
    assert len(intervals) == 36
    assert len({r['variant'] for r in intervals}) == 3
    assert {r['seed'] for r in intervals} == {'20261023', '20261024'}
    for r in intervals:
        assert float(r['ci95_lower']) <= float(r['ci95_upper'])
        assert float(r['family_ci_lower']) <= float(r['family_ci_upper'])
        assert math.isclose(float(r['family_alpha']), .05/6, abs_tol=1e-14)
        assert int(r['bootstrap_repetitions']) == 20000
    return dict(status='PASS', rate_bound_rows=len(rates), feasible_effect_bound_rows=len(effects),
                recorded_baseline_checks=len(checks), population_interval_rows=len(intervals),
                scope='Algebra and recorded-summary consistency only; no raw scan or new interval estimation')

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    print(json.dumps(verify(p.parse_args().root.resolve())))
