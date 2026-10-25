"""Explicit engineering preferences over already guarded reference comparisons.

No learned listener or quality probability is implied. This policy ranks only
the unchanged v1 evaluator's eligible choices and retains abstention when
the answer depends on modest changes in family priorities. It is deliberately
independent of candidate names, audio gain, and candidate order.
"""
from __future__ import annotations

from .mix_reference import POLICY as REFERENCE_POLICY
from .mix_reference import select_candidates

POLICY = {
    'kind': 'wcfi_joint_mix_reference_rank_v1',
    'guard_kind': REFERENCE_POLICY['kind'],
    'dimensions': dict(REFERENCE_POLICY['improvement']),
    'normalization': '(baseline error minus candidate error) / fixed meaningful improvement',
    'positive_improvement_cap': 3.,
    'negative_improvement': 'Retain all negative values; never floor adverse changes to zero',
    'families': {
        'spectrum': ['spectrum_db'],
        'dynamics': ['envelope_db', 'crest_db', 'dynamic_range_db'],
        'stereo': ['side_fraction', 'lr_correlation'],
    },
    'within_family_pooling': 'Arithmetic mean; correlated stereo measurements share one family',
    'weights': {
        'equal': {'spectrum': 1/3, 'dynamics': 1/3, 'stereo': 1/3},
        'spectrum_focus': {'spectrum': .5, 'dynamics': .25, 'stereo': .25},
        'dynamics_focus': {'spectrum': .25, 'dynamics': .5, 'stereo': .25},
        'stereo_focus': {'spectrum': .25, 'dynamics': .25, 'stereo': .5},
    },
    'minimum_margin': .10,
    'numeric_tolerance': REFERENCE_POLICY['numeric_tolerance'],
    'baseline': 'Zero utility and always a contender; even a sole candidate must clear the margin',
    'selection': 'Same unique winner in every weight scenario with minimum margin over all '
                 'other eligible choices and baseline; any unsupported candidate forces abstention',
    'dominated_candidates': 'Retain in margin comparisons: positive monotone utilities cannot '
                            'make a strictly dominated option better, but saturation or '
                            'near-equivalence must not create an artificially large margin',
    'display_order': 'Worst-case utility, then mean utility; equal values share rank. '
                     'IDs sort ties for display only, never for selection',
    'meaning': 'Capped dimensionless engineering utility, not human preference, perceived '
               'quality, a trained model score, a probability or confidence',
    'limits': 'Only comparable reference passages. No relaxation of v1 material-regression '
              'or meaningful-improvement guards. Validate on separate data and outcomes, '
              'not by reusing these utilities as truth. No automatic apply permission.',
}


def _ordered(values, *, epsilon):
    """Deterministic display ordering with explicit shared ranks for numerical ties."""
    ordered = sorted(values, key=lambda r: (-r['worst_case_utility'], -r['mean_utility'], r['id']))
    previous = None
    rank = 0
    result = []
    for position, row in enumerate(ordered, start=1):
        if (previous is None
                or abs(row['worst_case_utility']-previous['worst_case_utility']) > epsilon
                or abs(row['mean_utility']-previous['mean_utility']) > epsilon):
            rank = position
            previous = row
        result.append(row | {'rank': rank})
    return result


def rank_candidates(rows, baseline_id='unity'):
    """Rank ``{id, distances: mix_reference.compare(...)}`` rows without retuning.

    ``ordered_shortlist`` is a presentation order, never permission to apply its
    first item after an abstention. ``weight_results`` exposes every competing
    engineering preference, including all eligible candidates, not only the
    Pareto frontier. A robust winner also wins any convex combination of
    the declared weight vectors; this is algebraic stability, not confidence in
    musical preference or generalization.
    """
    rows = list(rows)
    guard = select_candidates(rows, baseline_id)
    result = {
        'kind': POLICY['kind'], 'status': 'abstain', 'selected_id': None,
        'baseline_id': baseline_id, 'eligible_ids': guard['eligible_ids'],
        'frontier_ids': guard['frontier_ids'], 'unknown_ids': guard['unknown_ids'],
        'rejected_ids': guard['rejected_ids'], 'guard_result': guard,
        'ordered_shortlist': [], 'weight_results': {}, 'minimum_winner_margin': None,
        'auto_apply_allowed': False,
    }
    if guard['reason'] == 'unsupported_baseline':
        return result | {'reason': 'unsupported_baseline'}
    if not guard['frontier_ids']:
        return result | {'reason': guard['reason']}
    epsilon = POLICY['numeric_tolerance']
    candidate_scores = {}
    for identifier in guard['eligible_ids']:
        delta = guard['evidence'][identifier]['improvement_from_baseline']
        normalized = {key: delta[key]/threshold for key, threshold in POLICY['dimensions'].items()}
        capped = {key: min(value, POLICY['positive_improvement_cap'])
                  for key, value in normalized.items()}
        family_scores = {family: sum(capped[k] for k in keys)/len(keys)
                         for family, keys in POLICY['families'].items()}
        scores = {name: sum(family_scores[k]*weight for k, weight in weights.items())
                  for name, weights in POLICY['weights'].items()}
        candidate_scores[identifier] = {
            'id': identifier, 'is_pareto_frontier': identifier in guard['frontier_ids'],
            'normalized_improvements': normalized,
            'capped_improvements': capped, 'family_utilities': family_scores,
            'weight_utilities': scores, 'worst_case_utility': min(scores.values()),
            'mean_utility': sum(scores.values())/len(scores),
        }
    result['ordered_shortlist'] = _ordered(candidate_scores.values(), epsilon=epsilon)
    winners = []
    margins = []
    for name in POLICY['weights']:
        values = {identifier: row['weight_utilities'][name]
                  for identifier, row in candidate_scores.items()}
        values[baseline_id] = 0.
        highest = max(values.values())
        top_ids = sorted(i for i, value in values.items() if highest-value <= epsilon)
        ranked = _ordered([{'id': i, 'worst_case_utility': value, 'mean_utility': value}
                           for i, value in values.items()], epsilon=epsilon)
        margin = None
        if len(top_ids) == 1:
            winner = top_ids[0]
            margin = values[winner]-max(v for i, v in values.items() if i != winner)
            winners.append(winner)
            margins.append(margin)
        else:
            winners.append(None)
        result['weight_results'][name] = {
            'top_ids': top_ids, 'winner_id': top_ids[0] if len(top_ids) == 1 else None,
            'margin': margin,
            'ranking': [{'id': r['id'], 'utility': r['mean_utility'], 'rank': r['rank']}
                        for r in ranked],
        }
    if guard['unknown_ids']:
        return result | {'reason': 'unsupported_candidates'}
    if None in winners:
        return result | {'reason': 'tied_weighted_ranking'}
    if baseline_id in winners:
        return result | {'reason': 'baseline_competitive'}
    if len(set(winners)) != 1:
        return result | {'reason': 'weight_sensitive_ranking'}
    result['minimum_winner_margin'] = min(margins)
    if min(margins)+epsilon < POLICY['minimum_margin']:
        return result | {'reason': 'insufficient_robust_margin'}
    return result | {'status': 'selected', 'reason': 'robust_guarded_engineering_preference',
                     'selected_id': winners[0]}
