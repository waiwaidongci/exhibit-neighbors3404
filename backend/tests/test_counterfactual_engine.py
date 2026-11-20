import random
import pytest

from src.services.counterfactual import (Model, Search, options_for, combinations_at_most,
                                         ranking_of, influence_order, verify)
from src.services.algorithm import fit_ratings


def demo_records(count=240):
    rng = random.Random(42)
    records = []
    for index in range(count):
        uid, slot = index // 6, index % 6
        exhibit = (uid % 3 * 3 + slot) % 12
        rating = round(max(1, min(5, 3 + (1 if exhibit % 3 == uid % 3 else -.4) + rng.gauss(0, .55))), 1)
        records.append({'id': index + 1, 'source_id': 1,
                        'reference': 'WP-202611-01-%04d' % (index + 1),
                        'values': {'visitor': 'V%03d' % (uid + 1),
                                   'exhibit': 'E%02d' % (exhibit + 1), 'rating': rating}})
    return records


def exact_rank(records, users, visitor, k=5, mc=2):
    return ranking_of(users, visitor, k, mc)


def test_options_respect_bounds():
    assert options_for(3.0) == (2.0, 4.0)
    assert options_for(1.0) == (2.0,)
    assert options_for(5.0) == (4.0,)
    assert options_for(1.5) == (1.0, 2.5)
    assert options_for(4.4) == (3.4, 5.0)
    for rating in (1.0, 1.2, 2.5, 3.9, 4.4, 5.0):
        values = options_for(rating)
        assert 1 <= len(values) <= 2
        assert all(1 <= v <= 5 and 0 < abs(v - rating) <= 1 + 1e-12 for v in values)


def test_combinations_counts_match_enumeration():
    for counts in ([2, 2, 2], [1, 2], [2, 2, 1, 2], [1]):
        dp = combinations_at_most(counts)
        brute = [0] * (len(counts) + 1)
        import itertools
        for size in range(len(counts) + 1):
            for combo in itertools.combinations(range(len(counts)), size):
                total = 1
                for i in combo:
                    total *= counts[i]
                brute[size] += total
        assert dp == brute


@pytest.mark.parametrize('visitor', ['V001', 'V009', 'V020'])
def test_mirror_matches_production_predictor(visitor):
    records = demo_records()
    model = Model(records, visitor, 5, 2)
    rng = random.Random(11)
    for _ in range(300):
        size = rng.choice([0, 1, 2, 3, 5])
        chosen = rng.sample(range(len(records)), size)
        edits = {i: rng.choice(options_for(records[i]['values']['rating'])) for i in chosen}
        users = {u: dict(items) for u, items in model.users.items()}
        for index, new in edits.items():
            values = records[index]['values']
            users[values['visitor']][values['exhibit']] = new
        mirrored = model.rank(edits)
        reference = exact_rank(records, users, visitor)
        for (e1, p1, _), (e2, p2, _) in zip(mirrored, reference):
            assert e1 == e2
            assert abs(p1 - p2) < 1e-9


def test_search_reaches_minimal_plan_and_proves_bound():
    records = demo_records()
    model = Model(records, 'V001', 5, 2)
    order = influence_order(model, records, list(range(len(records))))
    search = Search(model, records, order)
    search.run(20000)
    assert search.status() == 'completed'
    assert search.best['size'] == 2
    assert search.lower_bound == 2
    assert search.checked[0] == 1
    assert search.checked[1] == search.counts[1]
    state = search.state()
    checks = verify(records, 'V001', {'neighbors': 5, 'min_common': 2}, search.best, state)
    assert all(c['ok'] for c in checks)
    assert search.best['after_top'] != search.best['base_top']


def test_search_never_counts_twice_when_resumed():
    records = demo_records()
    model = Model(records, 'V001', 5, 2)
    order = influence_order(model, records, list(range(len(records))))
    full = Search(model, records, order)
    full.run(20000)
    # walk the same search in tiny commits, persisting/reloading state
    incremental = Search(model, records, order)
    for _ in range(5000):
        incremental.run(1)
        if incremental.status() in ('completed', 'none_found'):
            break
        saved = incremental.state()
        incremental = Search(model, records, order, saved)
    assert incremental.status() == full.status()
    assert incremental.checked == full.checked
    assert incremental.lower_bound == full.lower_bound
    assert (incremental.best or {}).get('size') == (full.best or {}).get('size')


def test_paused_search_reports_honest_bound():
    records = demo_records()
    model = Model(records, 'V001', 5, 2)
    order = influence_order(model, records, list(range(len(records))))
    search = Search(model, records, order)
    search.run(50)
    assert search.status() in ('paused', 'completed')
    if search.status() == 'paused':
        assert '未证最小' in search.proof() or search.best is None
        assert search.lower_bound == search.exhausted_depth + 1
