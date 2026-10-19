#!/usr/bin/env python3
"""Reproduce the maintenance dispatch audit using Python 3.10+ standard library.
Only data/maintenance_tasks.csv is an empirical input. No network or ML packages.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COLUMNS = ['task_id', 'dispatch_time', 'telemetry_synced_at', 'plant_zone',
           'device_type', 'reported_health_score', 'continuous_run_hours', 'task_success']
LOCAL_TZ = timezone(timedelta(hours=8))
WASTED_HOURS = 120
VERIFY_HOURS = 15


def parse_aware(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('timezone is required')
    return dt


def prepare(records):
    """Drop exact duplicates. Quarantine all versions of a conflicting task ID.
    Missing outcomes remain None; they are never converted to failures/successes.
    """
    unique = {}
    row_map = defaultdict(list)
    audit = []
    for line, raw_row in enumerate(records, 2):
        row = {c: raw_row.get(c, '') for c in COLUMNS}
        key = tuple(row[c] for c in COLUMNS)
        row_map[key].append(line)
        if key in unique:
            audit.append({'source_row': line, 'task_id': row['task_id'], 'reason': 'exact_duplicate'})
        else:
            unique[key] = row
    id_versions = Counter(row['task_id'] for row in unique.values())
    conflicts = {k for k, n in id_versions.items() if n > 1}
    clean = []
    invalid = Counter()
    for key, row in unique.items():
        reason = None
        item = dict(row)
        item['source_row'] = row_map[key][0]
        try:
            if not row['task_id'] or not row['plant_zone'] or not row['device_type']:
                raise ValueError('missing_id_or_category')
            if row['task_id'] in conflicts:
                raise ValueError('conflicting_task_id')
            d, s = parse_aware(row['dispatch_time']), parse_aware(row['telemetry_synced_at'])
            age = (d - s).total_seconds() / 60
            if age < 0:
                raise ValueError('telemetry_after_dispatch')
            score = float(row['reported_health_score'])
            run_hours = float(row['continuous_run_hours'])
            if not math.isfinite(score) or not math.isfinite(run_hours) or run_hours < 0:
                raise ValueError('invalid_numeric_value')
            label = row['task_success']
            if label not in ('0', '1', ''):
                raise ValueError('invalid_outcome')
            item.update(telemetry_age_minutes=age, reported_health_score=score,
                        continuous_run_hours=run_hours,
                        success=None if label == '' else int(label),
                        outcome_missing=int(label == ''),
                        run_hours_definition_conflict=int(run_hours > 24),
                        dispatch_date=d.astimezone(LOCAL_TZ).date().isoformat())
        except (ValueError, TypeError) as exc:
            reason = str(exc)
        if reason:
            invalid[reason] += 1
            audit.append({'source_row': item['source_row'], 'task_id': row['task_id'], 'reason': reason})
        else:
            clean.append(item)
    quality = {
        'raw_rows': len(records), 'exact_duplicates_removed': len(records) - len(unique),
        'unique_task_ids_raw': len(id_versions), 'conflicting_task_ids': len(conflicts),
        'quarantined_unique_rows': sum(invalid.values()), 'invalid_reasons': dict(invalid),
        'usable_unique_tasks': len(clean), 'labeled_tasks': sum(r['success'] is not None for r in clean),
        'missing_outcomes': sum(r['success'] is None for r in clean),
        'run_hours_over_24': sum(r['continuous_run_hours'] > 24 for r in clean),
        'raw_missing_outcomes': sum(r['task_success'] == '' for r in records),
    }
    return clean, quality, audit


@dataclass(frozen=True)
class Rule:
    family: str
    threshold: float | None = None
    zone: str | None = None
    device: str | None = None

    def selects(self, row):
        if self.family == 'none':
            return False
        if self.family == 'all':
            return True
        if self.family in ('age', 'age_exclude_zone'):
            return row['telemetry_age_minutes'] >= self.threshold and (self.zone is None or row['plant_zone'] != self.zone)
        if self.family == 'health':
            return row['reported_health_score'] <= self.threshold
        if self.family == 'run_hours_diagnostic_only':
            return row['continuous_run_hours'] >= self.threshold
        if self.family == 'zone':
            return row['plant_zone'] == self.zone
        if self.family == 'device':
            return row['device_type'] == self.device
        raise ValueError(self.family)

    def complexity(self):
        return 2 if self.family == 'age_exclude_zone' else 1


def wilson(k, n, z=1.959963984540054):
    if not n:
        return [None, None]
    p = k / n
    denominator = 1 + z*z/n
    center = (p + z*z/(2*n)) / denominator
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denominator
    return [max(0.0, center-half), min(1.0, center+half)]


def evaluate(rows, rule, q=1.0):
    if not 0 <= q <= 1:
        raise ValueError('q must be between 0 and 1')
    if any(r['success'] is None for r in rows):
        raise ValueError('evaluate requires observed outcomes; use population_bounds for missing outcomes')
    selected = [r for r in rows if rule.selects(r)]
    failures = sum(r['success'] == 0 for r in rows)
    caught = sum(r['success'] == 0 for r in selected)
    checks = len(selected)
    baseline = failures * WASTED_HOURS
    verification = checks * VERIFY_HOURS
    avoided = caught * WASTED_HOURS * q
    return {
        'n': len(rows), 'failures': failures, 'checks': checks, 'caught_failures': caught,
        'checked_successes': checks-caught, 'unchecked_failures': failures-caught,
        'unchecked_successes': len(rows)-checks-(failures-caught),
        'check_rate': checks/len(rows) if rows else None,
        'precision': caught/checks if checks else None,
        'precision_wilson95': wilson(caught, checks),
        'failure_recall': caught/failures if failures else None,
        'baseline_hours': baseline, 'verification_hours': verification,
        'q_assumed': q, 'avoided_wasted_hours': avoided,
        'residual_wasted_hours': baseline-avoided,
        'total_hours': baseline-avoided+verification,
        'net_saved_hours': avoided-verification,
        'saved_fraction': (avoided-verification)/baseline if baseline else None,
        'break_even_q': verification/(caught*WASTED_HOURS) if caught else None,
    }


def population_bounds(rows, rule, q=1.0):
    known = [r for r in rows if r['success'] is not None]
    unknown = [r for r in rows if r['success'] is None]
    m = evaluate(known, rule, q)
    unknown_checks = sum(rule.selects(r) for r in unknown)
    checks = m['checks'] + unknown_checks
    lower = q*WASTED_HOURS*m['caught_failures'] - VERIFY_HOURS*checks
    upper = lower + q*WASTED_HOURS*unknown_checks
    return {'n': len(rows), 'known_outcomes': len(known), 'unknown_outcomes': len(unknown),
            'checks_including_unknown': checks, 'unknown_checks': unknown_checks,
            'verification_hours_including_unknown': VERIFY_HOURS*checks,
            'conservative_break_even_q': VERIFY_HOURS*checks/(WASTED_HOURS*m['caught_failures']) if m['caught_failures'] else None,
            'net_saved_hours_bounds': [lower, upper],
            'observed_failure_rate_bounds': [m['failures']/len(rows),
                                           (m['failures']+len(unknown))/len(rows)] if rows else [None, None]}


def candidates(train):
    zones = sorted({r['plant_zone'] for r in train})
    devices = sorted({r['device_type'] for r in train})
    result = [Rule('none'), Rule('all')]
    for t in range(0, 121, 5):
        result.append(Rule('age', t))
        result.extend(Rule('age_exclude_zone', t, zone=z) for z in zones)
    # This supplied file has integer scores 0..30. Bounds use training features only.
    lo = math.floor(min(r['reported_health_score'] for r in train))
    hi = math.ceil(max(r['reported_health_score'] for r in train))
    result.extend(Rule('health', t) for t in range(lo, hi+1))
    result.extend(Rule('zone', zone=z) for z in zones)
    result.extend(Rule('device', device=d) for d in devices)
    return result


def rank_rules(train, rules):
    values = [(rule, evaluate(train, rule)) for rule in rules]
    return sorted(values, key=lambda x: (-x[1]['net_saved_hours'], x[1]['checks'],
                  x[0].complexity(), x[0].threshold if x[0].threshold is not None else -1,
                  x[0].family, x[0].zone or '', x[0].device or ''))


def summarize(rows):
    observed = [r for r in rows if r['success'] is not None]
    f = sum(r['success'] == 0 for r in observed)
    return {'n': len(rows), 'labeled': len(observed), 'missing': len(rows)-len(observed),
            'failures': f, 'failure_rate': f/len(observed) if observed else None,
            'failure_rate_wilson95': wilson(f, len(observed))}


def grouped(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return [{'group': k, **summarize(g)} for k, g in sorted(groups.items())]


def cross_groups(rows, field, threshold):
    output = []
    for value in sorted({r[field] for r in rows}):
        g = [r for r in rows if r[field] == value]
        fresh = summarize([r for r in g if r['telemetry_age_minutes'] < threshold])
        stale = summarize([r for r in g if r['telemetry_age_minutes'] >= threshold])
        output.append({'group': value, 'fresh_n': fresh['labeled'], 'fresh_failures': fresh['failures'],
                       'fresh_rate': fresh['failure_rate'], 'fresh_ci': fresh['failure_rate_wilson95'],
                       'stale_n': stale['labeled'], 'stale_failures': stale['failures'],
                       'stale_rate': stale['failure_rate'], 'stale_ci': stale['failure_rate_wilson95']})
    return output


def write_csv(path, rows, columns=None):
    if not columns:
        columns = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                             for k, v in row.items()})


def draw_svg(path, age_bins, holdout, rule):
    # Direct SVG avoids font, plotting package and platform dependencies.
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="620" viewBox="0 0 1100 620" role="img" aria-labelledby="title desc">',
             '<title id="title">Telemetry age and historical policy economics</title>',
             '<desc id="desc">Failure rates and Wilson intervals by telemetry age; holdout savings under explicitly assumed verification effectiveness.</desc>',
             '<rect width="1100" height="620" fill="#f5f7fb"/>',
             '<style>text{font-family:Arial,sans-serif;fill:#24324b}.title{font-size:24px;font-weight:700}.label{font-size:14px}.small{font-size:12px}</style>',
             '<text x="40" y="40" class="title">Dispatch decisions need fresh equipment state</text>',
             '<text x="40" y="65" class="label">Supplied data only. Associations and conditional backtests; no measured intervention effect.</text>',
             '<rect x="25" y="90" width="520" height="450" rx="12" fill="white"/>',
             '<rect x="560" y="90" width="515" height="450" rx="12" fill="white"/>',
             '<text x="45" y="122" class="label">Observed failure rate by telemetry age (95% Wilson CI)</text>',
             '<text x="580" y="122" class="label">Holdout net saved labor hours vs. no verification</text>']
    for tick in range(0, 81, 20):
        y = 435 - tick*3.2
        parts.extend([f'<line x1="80" y1="{y}" x2="520" y2="{y}" stroke="#e3e9f0"/>',
                      f'<text x="42" y="{y+5}" class="small">{tick}%</text>'])
    for i, b in enumerate(age_bins):
        x = 90 + i*54
        if b['failure_rate'] is None:
            continue
        rate = b['failure_rate']
        lo, hi = b['failure_rate_wilson95']
        y = 435 - rate*320
        color = '#d27a27' if i >= 3 else '#287e9b'
        parts.extend([f'<rect x="{x}" y="{y:.2f}" width="30" height="{rate*320:.2f}" fill="{color}"/>',
                      f'<line x1="{x+15}" y1="{435-hi*320:.2f}" x2="{x+15}" y2="{435-lo*320:.2f}" stroke="#293851" stroke-width="2"/>',
                      f'<text x="{x+15}" y="{435-hi*320-10:.2f}" text-anchor="middle" class="small">{rate:.0%}</text>',
                      f'<text x="{x+15}" y="457" text-anchor="middle" class="small">{escape(b["group"])}</text>',
                      f'<text x="{x+15}" y="479" text-anchor="middle" class="small">n={b["labeled"]}</text>'])
    parts.append('<text x="250" y="510" class="small">Telemetry age at dispatch (minutes)</text>')
    points = []
    for q in (0, .25, .5, .75, 1):
        value = evaluate(holdout, rule, q)['net_saved_hours']
        x, y = 610+q*420, 430-(value+2500)/7000*250
        points.append(f'{x:.2f},{y:.2f}')
        parts.extend([f'<circle cx="{x}" cy="{y:.2f}" r="5" fill="#287e9b"/>',
                      f'<text x="{x}" y="{y-13:.2f}" text-anchor="middle" class="small">{value:,.0f}</text>',
                      f'<text x="{x}" y="457" text-anchor="middle" class="small">{q:.0%}</text>'])
    zero_y = 430-2500/7000*250
    parts.extend([f'<line x1="600" y1="{zero_y:.2f}" x2="1040" y2="{zero_y:.2f}" stroke="#9baabd" stroke-dasharray="4 4"/>',
                  f'<polyline points="{" ".join(points)}" fill="none" stroke="#287e9b" stroke-width="3"/>',
                  '<text x="665" y="510" class="small">Assumed avoidance effectiveness q (not observed)</text>',
                  '<text x="40" y="580" class="label">At 15 minutes, verify before travel. Keep urgent work moving through an explicit exception path.</text>',
                  '<text x="40" y="604" class="small">120 labor-hours per wasted trip; 15 per verification. The verification action itself is absent from the dataset.</text>', '</svg>'])
    path.write_text('\n'.join(parts), encoding='utf-8')


def analyze(input_path, output, split_date):
    payload = input_path.read_bytes()
    with input_path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != COLUMNS:
            raise ValueError(f'Expected columns {COLUMNS}; got {reader.fieldnames}')
        records = list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in records):
            raise ValueError('Malformed CSV row length')
    clean, quality, audit = prepare(records)
    if not clean:
        raise ValueError('No usable tasks')
    labeled = sorted((r for r in clean if r['success'] is not None),
                     key=lambda r: (parse_aware(r['dispatch_time']), r['task_id']))
    train = [r for r in labeled if r['dispatch_date'] < split_date]
    test = [r for r in labeled if r['dispatch_date'] >= split_date]
    if not train or not test:
        raise ValueError('Temporal split must have observed outcomes on both sides')
    ranking = rank_rules(train, candidates(train))
    rule, training = ranking[0]
    diagnostic_run_rules = rank_rules(train, [Rule('run_hours_diagnostic_only', t) for t in range(0, 121, 5)])
    training_rule_ids = {r['task_id'] for r in train if rule.selects(r)}
    equivalent = [t for t in range(121) if rule.family == 'age' and
                  {r['task_id'] for r in train if Rule('age', t).selects(r)} == training_rule_ids]
    selected = [r for r in labeled if rule.selects(r)]
    unselected = [r for r in labeled if not rule.selects(r)]
    boundaries = [0, 5, 10, 15, 20, 30, 45, 60, math.inf]
    age_bins = []
    for lo, hi in zip(boundaries, boundaries[1:]):
        g = [r for r in clean if lo <= r['telemetry_age_minutes'] < hi]
        age_bins.append({'group': f'{lo}-{int(hi)}' if math.isfinite(hi) else f'{lo}+', **summarize(g)})
    by_zone = cross_groups(labeled, 'plant_zone', rule.threshold)
    by_device = cross_groups(labeled, 'device_type', rule.threshold)
    health_buckets = [dict(r, health_bucket=f'{int(r["reported_health_score"]//10)*10}-{int(r["reported_health_score"]//10)*10+9}') for r in labeled]
    by_health = cross_groups(health_buckets, 'health_bucket', rule.threshold)
    by_day = grouped(clean, lambda r: r['dispatch_date'])
    baselines = [Rule('none'), Rule('all')]
    for family in ['health', 'zone', 'device']:
        baselines.append(next(r for r, m in ranking if r.family == family))
    baselines.append(diagnostic_run_rules[0][0])
    baselines.append(rule)
    comparison = [{'rule': asdict(r), 'train': evaluate(train, r), 'holdout': evaluate(test, r)} for r in baselines]
    sensitivities = [{'q': q, **evaluate(test, rule, q)} for q in (0, .25, .5, .75, 1)]
    threshold_sensitivity = [{'threshold_minutes': t, 'train': evaluate(train, Rule('age', t)),
                              'holdout': evaluate(test, Rule('age', t))} for t in (10, 15, 20, 25, 30)]
    result = {
        'source': {'path': 'data/maintenance_tasks.csv', 'sha256': hashlib.sha256(payload).hexdigest(),
                   'bytes': len(payload), 'local_date_min': min(r['dispatch_date'] for r in clean),
                   'local_date_max': max(r['dispatch_date'] for r in clean)},
        'quality': quality, 'overall': summarize(clean),
        'assumptions': {'wasted_trip_labor_hours': WASTED_HOURS, 'verification_labor_hours': VERIFY_HOURS,
                        'q': 'unknown; scenario parameter, not measured',
                        'training_selection_q': 1, 'no_synthetic_observations': True},
        'split_date_local': split_date, 'selected_rule': asdict(rule),
        'candidate_count': len(ranking), 'equivalent_age_thresholds_train': equivalent,
        'train': training, 'holdout': evaluate(test, rule), 'full_descriptive': evaluate(labeled, rule),
        'selected_group_full': summarize(selected), 'unselected_group_full': summarize(unselected),
        'population_bounds_full': population_bounds(clean, rule),
        'population_bounds_holdout': population_bounds([r for r in clean if r['dispatch_date'] >= split_date], rule),
        'age_bins': age_bins, 'zone_cross': by_zone, 'device_cross': by_device, 'health_cross': by_health,
        'by_day': by_day,
        'policy_by_day': [{'date': day, **evaluate([r for r in labeled if r['dispatch_date'] == day], rule)} for day in sorted({r['dispatch_date'] for r in labeled})],
        'baselines': comparison, 'effectiveness_sensitivity_holdout': sensitivities,
        'threshold_sensitivity': threshold_sensitivity,
        'limitations': ['Association does not identify a causal telemetry/cache/network defect.',
                       'Global exploratory summaries were viewed before the temporal backtest; holdout is not a pristine preregistered test.',
                       'Seven days and no device identifier prevent long-term or repeated-device validation.',
                       'Verification effectiveness, false alarms, elapsed delay, urgency SLA and capacity are not observed.',
                       'continuous_run_hours contradicts the supplied same-day definition for most tasks.'],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output/'metrics.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    (output/'policy.json').write_text(json.dumps({'rule': asdict(rule), 'trained_before_local_date': split_date,
             'assumed_q_for_training': 1, 'candidate_count': len(ranking)}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    write_csv(output/'cleaned_tasks.csv', clean, COLUMNS + ['source_row', 'telemetry_age_minutes', 'success', 'outcome_missing', 'run_hours_definition_conflict', 'dispatch_date'])
    write_csv(output/'quality_audit.csv', audit, ['source_row', 'task_id', 'reason'])
    write_csv(output/'age_bins.csv', age_bins)
    write_csv(output/'zone_cross.csv', by_zone)
    write_csv(output/'device_cross.csv', by_device)
    write_csv(output/'health_cross.csv', by_health)
    write_csv(output/'daily.csv', by_day)
    write_csv(output/'policy_by_day.csv', result['policy_by_day'])
    write_csv(output/'candidate_search_train.csv', [{**asdict(r), **m} for r, m in ranking])
    write_csv(output/'policy_evaluation.csv', [{'cohort': name, 'policy': r.family, 'threshold': r.threshold,
             'zone': r.zone, 'device': r.device, **evaluate(data, r)}
             for name, data in [('train', train), ('holdout', test)] for r in baselines])
    write_csv(output/'effectiveness_sensitivity.csv', sensitivities)
    write_csv(output/'threshold_sensitivity.csv', [{'threshold_minutes': r['threshold_minutes'],
             'train_net_saved_hours': r['train']['net_saved_hours'], 'holdout_net_saved_hours': r['holdout']['net_saved_hours'],
             'holdout_checks': r['holdout']['checks'], 'holdout_caught_failures': r['holdout']['caught_failures']} for r in threshold_sensitivity])
    decisions = []
    for row in sorted(clean, key=lambda r: (parse_aware(r['dispatch_time']), r['task_id'])):
        check = rule.selects(row)
        decisions.append({'task_id': row['task_id'], 'dispatch_time': row['dispatch_time'],
             'telemetry_age_minutes': row['telemetry_age_minutes'], 'plant_zone': row['plant_zone'],
             'task_success': row['task_success'], 'cohort': 'train' if row['dispatch_date'] < split_date else 'holdout',
             'verify_before_departure': int(check),
             'historical_failure_in_selected_group': None if row['success'] is None else int(check and row['success'] == 0),
             'reason': f'telemetry_age_minutes >= {rule.threshold}' if check else 'below_selected_age_threshold'})
    write_csv(output/'task_decisions.csv', decisions)
    draw_svg(output/'overview.svg', age_bins, test, rule)
    print(json.dumps({k: result[k] for k in ['quality', 'selected_rule', 'train', 'holdout', 'population_bounds_holdout']}, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT/'data'/'maintenance_tasks.csv')
    parser.add_argument('--output', type=Path, default=ROOT/'reports')
    parser.add_argument('--split-date', default='2026-09-05', help='Local UTC+8 date; train is strictly before it')
    args = parser.parse_args()
    date.fromisoformat(args.split_date)
    analyze(args.input, args.output, args.split_date)
