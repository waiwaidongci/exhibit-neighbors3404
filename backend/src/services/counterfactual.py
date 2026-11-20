"""Counterfactual rating search for a completed neighbours study.

The question: using the frozen ratings of one completed run, which recorded
ratings could have been mis-entered by one star, and how few such corrections
are enough to move the chosen visitor's first recommendation to another
exhibit?

Model rules (kept explicit so any conclusion can be recomputed by hand):

* matrix, visitor and parameters come from a completed run snapshot;
* only ratings in the declared scope may change;
* one change moves one rating by at most one star (``r-1`` or ``r+1``) and
  keeps it within 1–5, so a rating near a bound moves to that bound by the
  remaining fraction; the count of options is one or two per rating;
* a plan is a set of changes; its size is the number of changed ratings;
* a flip means the visitor's top predicted exhibit differs from the base top;
  ties break on exhibit code exactly as the production predictor does.

``services.algorithm.predict`` remains the authority.  A pure-Python mirror is
used to screen plans quickly; every plan the mirror calls a flip, or whose
leading margin is near a tie, is recomputed with the production function
before it is accepted.
"""
import hashlib
import itertools
import json
import math
from collections import defaultdict

from .algorithm import fit_ratings, predict as reference_predict

TIE_BAND = 1e-7  # margins below this always trigger an exact re-check


def options_for(rating):
    """Allowed corrections of at most one star for one rating, ascending."""
    rating = float(rating)
    out = []
    for value in (max(1.0, rating - 1.0), min(5.0, rating + 1.0)):
        if 1.0 <= value <= 5.0 and abs(value - rating) > 1e-12:
            out.append(round(value, 10))
    return tuple(sorted(set(out)))


def _mean(values):
    values = list(values)
    return float(sum(values) / len(values)) if values else 0.0


def ranking_of(users, visitor, neighbors, min_common):
    """Production ranking of every unrated exhibit for one visitor."""
    exhibits = sorted({e for items in users.values() for e in items})
    rated = set(users.get(visitor, {}))
    out = []
    for exhibit in exhibits:
        if exhibit in rated:
            continue
        prediction, _, method = reference_predict(users, visitor, exhibit, int(neighbors), int(min_common))
        out.append((exhibit, prediction, method))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out


class Model:
    """Incremental pure-Python mirror of ``predict`` for one visitor."""

    def __init__(self, records, visitor, neighbors, min_common):
        self.k = int(neighbors)
        self.mc = int(min_common)
        self.visitor = visitor
        self.records = records
        self.users = fit_ratings([r['values'] for r in records])
        if visitor not in self.users:
            raise ValueError('待查访客不在该研究快照中')
        self.t = self.users[visitor]
        self.tkeys = set(self.t)
        self.tsize = len(self.t)
        if self.tsize < 1:
            raise ValueError('待查访客没有任何评分')
        self.tmean = _mean(self.t.values())
        self.sizes = {u: len(v) for u, v in self.users.items()}
        self.sums = {u: float(sum(v.values())) for u, v in self.users.items()}
        self.means = {u: self.sums[u] / self.sizes[u] for u in self.users}
        self.total = float(sum(self.sums.values()))
        self.count = sum(self.sizes.values())
        item_sum = defaultdict(float)
        item_count = defaultdict(int)
        for items in self.users.values():
            for exhibit, value in items.items():
                item_sum[exhibit] += value
                item_count[exhibit] += 1
        self.item_sum = dict(item_sum)
        self.item_count = dict(item_count)
        self.exhibits = sorted(item_count)
        self.candidates = [e for e in self.exhibits if e not in self.tkeys]
        self.common = {}
        for user, items in self.users.items():
            if user == visitor:
                continue
            shared = sorted(self.tkeys & set(items))
            if len(shared) >= self.mc:
                self.common[user] = shared
        self.eligible = {e: [u for u in self.common if e in self.users[u]] for e in self.candidates}
        self.base_sims = {u: self._sim(c, None, None, None, u) for u, c in self.common.items()}
        self._base_rank = self.rank({})

    def _sim(self, shared, tvals, tmean, user_mean, user, value_at=None):
        if tvals is None:
            a = [self.t[i] - self.tmean for i in shared]
        else:
            a = [tvals[i] - tmean for i in shared]
        if user_mean is None:
            b = [self.users[user][i] - self.means[user] for i in shared]
        elif value_at is None:
            b = [self.users[user][i] - user_mean for i in shared]
        else:
            b = [value_at.get((user, i), self.users[user][i]) - user_mean for i in shared]
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        den = na * nb
        return sum(x * y for x, y in zip(a, b)) / den if den > 0 else 0.0

    def rank(self, edits):
        """Prediction for every candidate exhibit under ``edits``.

        ``edits`` maps record index -> new rating.  The list is sorted exactly
        like the production code: descending prediction, then exhibit code.
        """
        value_at = {}
        delta_user = defaultdict(float)
        delta_item = defaultdict(float)
        delta_total = 0.0
        for index, new in edits.items():
            values = self.records[index]['values']
            user, exhibit, old = values['visitor'], values['exhibit'], float(values['rating'])
            value_at[(user, exhibit)] = float(new)
            delta_user[user] += float(new) - old
            delta_item[exhibit] += float(new) - old
            delta_total += float(new) - old
        target_edited = self.visitor in delta_user
        tmean = self.tmean + delta_user.get(self.visitor, 0.0) / self.tsize
        tvals = None
        if target_edited:
            tvals = {e: value_at.get((self.visitor, e), self.t[e]) for e in self.t}
        global_mean = (self.total + delta_total) / self.count if self.count else 3.0
        sims = dict(self.base_sims)
        means = dict(self.means)
        for user, delta in delta_user.items():
            means[user] = self.means[user] + delta / self.sizes[user]
        if target_edited:
            changed = [u for u in self.common]
        else:
            changed = [u for u in delta_user if u in self.common]
        for user in changed:
            if target_edited and user in delta_user:
                sims[user] = self._sim(self.common[user], tvals, tmean, means[user], user, value_at)
            elif target_edited:
                sims[user] = self._sim(self.common[user], tvals, tmean, None, user)
            else:
                sims[user] = self._sim(self.common[user], None, None, means[user], user, value_at)

        def effective(user, exhibit):
            return value_at.get((user, exhibit), self.users[user][exhibit])

        out = []
        for exhibit in self.candidates:
            count = self.item_count.get(exhibit, 0)
            fallback = ((self.item_sum.get(exhibit, 0.0) + delta_item.get(exhibit, 0.0)) / count
                        if count else global_mean)
            if self.tsize < 2:
                out.append((exhibit, min(5.0, max(1.0, fallback)), 'item' if count else 'global'))
                continue
            neighbors = []
            for user in self.eligible[exhibit]:
                sim = sims[user]
                if sim > 0:
                    neighbors.append((user, sim, effective(user, exhibit) - means[user]))
            neighbors.sort(key=lambda x: (-x[1], x[0]))
            neighbors = neighbors[:self.k]
            if not neighbors:
                out.append((exhibit, min(5.0, max(1.0, fallback)), 'item' if count else 'global'))
                continue
            prediction = tmean + sum(s * d for _, s, d in neighbors) / sum(s for _, s, _ in neighbors)
            out.append((exhibit, min(5.0, max(1.0, prediction)), 'neighbors'))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out

    @property
    def base_rank(self):
        return self._base_rank

    def margin(self, edits):
        ranked = self.rank(edits)
        if len(ranked) < 2:
            return 9.0
        return ranked[0][1] - ranked[1][1]


def scope_options(records, scope_indices):
    return {i: options_for(records[i]['values']['rating']) for i in scope_indices}


def influence_order(model, records, scope_indices):
    """Deterministic order that puts the most influential ratings first.

    Score = the largest movement of any candidate prediction caused by a
    single one-star correction of that rating.  Ties break on reference so the
    same scope always enumerates in the same order.
    """
    # Keep job creation responsive on very wide scopes: the order is only a
    # heuristic, correctness never depends on it.
    if len(scope_indices) * max(1, len(model.candidates)) > 400000:
        return sorted(scope_indices, key=lambda i: records[i]['reference'])
    base = {e: p for e, p, _ in model.base_rank}
    scored = []
    for index in scope_indices:
        values = records[index]['values']
        best = 0.0
        for value in options_for(values['rating']):
            for exhibit, prediction, _ in model.rank({index: value}):
                best = max(best, abs(prediction - base[exhibit]))
        scored.append((round(best, 12), records[index]['reference'], index))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [index for _, _, index in scored]


def scope_fingerprint(run_checksum, visitor, parameters, records, scope_refs):
    by_ref = {r['reference']: r for r in records}
    payload = {
        'run_checksum': run_checksum,
        'visitor': visitor,
        'parameters': {'neighbors': int(parameters['neighbors']), 'min_common': int(parameters['min_common'])},
        'ratings': sorted(
            [by_ref[ref]['reference'], by_ref[ref]['values']['visitor'],
             by_ref[ref]['values']['exhibit'], round(float(by_ref[ref]['values']['rating']), 10)]
            for ref in scope_refs
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def combinations_at_most(option_counts):
    """number[d] = how many distinct plans of size d the scope allows."""
    counts = [1]
    for size in option_counts:
        extended = counts + [0] * size
        for i in range(len(counts) - 1, -1, -1):
            if counts[i]:
                extended[i + 1] += counts[i] * size
        counts = extended
    return counts[:len(option_counts) + 1]


class Search:
    """Exhaustive, budgeted, resumable search over plans of growing size.

    Search order is fixed: size 0, then every size-1 plan, then every size-2
    plan, and so on.  ``cursor`` names the next unchecked plan, and ``checked``
    counts only plans finished inside committed chunks; a retry therefore
    re-walks an unfinished chunk without counting anything twice.
    """

    def __init__(self, model, records, order, state=None):
        self.model = model
        self.records = records
        self.order = list(order)
        self.scope = list(order)
        self.options = scope_options(records, self.scope)
        self.option_counts = [len(self.options[i]) for i in self.scope]
        self.counts = combinations_at_most(self.option_counts)
        self.max_depth = len(self.scope)
        state = state or {}
        self.depth = int(state.get('depth', 0))
        self.cursor = state.get('cursor')
        self.checked = {int(k): int(v) for k, v in (state.get('checked') or {}).items()}
        self.exhausted_depth = int(state.get('exhausted_depth', -1))
        self.best = state.get('best')
        self.nodes = int(state.get('nodes', 0))
        self.near_misses = state.get('near_misses') if isinstance(state.get('near_misses'), dict) else {}
        self.guided_used = int(state.get('guided_used', 0))
        self.guided_targets = list(state.get('guided_targets') or [])
        self.guided_done = bool(state.get('guided_done', False))
        self.lower_bound = self.exhausted_depth + 1

    # -- enumeration ------------------------------------------------------
    def _plans(self, depth, cursor):
        n = len(self.scope)
        if depth == 0:
            if cursor is None:
                yield (), 0, ()
            return
        if depth > n:
            return
        if cursor is None:
            combo = tuple(range(depth))
            value_start = 0
            if depth == 0:
                return
        else:
            combo = tuple(cursor['indices'])
            value_start = int(cursor['value'])
        while True:
            choices = [self.options[self.scope[i]] for i in combo]
            for position, values in enumerate(itertools.product(*choices)):
                if position < value_start:
                    continue
                yield combo, position, values
            advance = list(combo)
            j = depth - 1
            while j >= 0:
                if advance[j] < n - (depth - j):
                    advance[j] += 1
                    for k in range(j + 1, depth):
                        advance[k] = advance[k - 1] + 1
                    break
                j -= 1
            if j < 0:
                return
            combo = tuple(advance)
            value_start = 0

    def _edits(self, combo, values):
        return {self.scope[combo[j]]: values[j] for j in range(len(combo))}

    # -- authoritative checks --------------------------------------------
    def exact_rank(self, edits):
        users = {u: dict(items) for u, items in self.model.users.items()}
        for index, new in edits.items():
            values = self.records[index]['values']
            users[values['visitor']][values['exhibit']] = float(new)
        return ranking_of(users, self.model.visitor, self.model.k, self.model.mc), users

    def base_ranking(self):
        return ranking_of(self.model.users, self.model.visitor, self.model.k, self.model.mc)

    def _base_top(self):
        if not hasattr(self, '_base_top_value'):
            self._base_top_value = self.base_ranking()[0][0]
        return self._base_top_value

    def _confirm(self, edits, ranked=None):
        """Return the exact ranking if the plan really flips the base top."""
        base_top = self._base_top()
        ranked = ranked if ranked is not None else self.model.rank(edits)
        if not ranked:
            return None
        near_tie = len(ranked) > 1 and ranked[0][1] - ranked[1][1] < TIE_BAND
        if ranked[0][0] == base_top and not near_tie:
            return None
        exact, _ = self.exact_rank(edits)
        if exact and exact[0][0] != base_top:
            return exact
        return None

    def make_plan(self, edits, exact):
        base = self.base_ranking()
        rows = []
        for index in sorted(edits, key=lambda i: self.records[i]['reference']):
            values = self.records[index]['values']
            rows.append({
                'record_index': index,
                'reference': self.records[index]['reference'],
                'visitor': values['visitor'],
                'exhibit': values['exhibit'],
                'from': float(values['rating']),
                'to': float(edits[index]),
            })
        rows.sort(key=lambda r: r['reference'])
        return {
            'size': len(rows),
            'edits': rows,
            'visitor': self.model.visitor,
            'parameters': {'neighbors': self.model.k, 'min_common': self.model.mc},
            'base_ranking': [{'exhibit': e, 'prediction': p} for e, p, _ in base[:3]],
            'after_ranking': [{'exhibit': e, 'prediction': p} for e, p, _ in exact[:3]],
            'base_top': base[0][0],
            'after_top': exact[0][0],
            'method': exact[0][2],
            'edited_checksum': _edited_checksum(self.model.users, edits, self.records),
        }

    def _keep(self, plan, source):
        """Keep the cheapest plan; ties prefer the deterministic exhaustive plan."""
        plan = dict(plan, source=source)
        if not self.best:
            self.best = plan
            return plan
        if plan['size'] < self.best['size']:
            self.best = plan
            return plan
        if plan['size'] == self.best['size'] and source == 'exhaustive' and self.best['source'] != 'exhaustive':
            self.best = plan
            return plan
        return None

    def _note_near(self, combo, values, ranked):
        if len(ranked) < 2:
            return
        margin = ranked[0][1] - ranked[1][1]
        key = str(len(combo))
        stored = self.near_misses.get(key)
        if stored is None or margin < stored['margin']:
            self.near_misses[key] = {
                'margin': margin,
                'top': ranked[0][0],
                'runner_up': ranked[1][0],
                'plan': [
                    {'reference': self.records[self.scope[combo[j]]]['reference'],
                     'from': float(self.records[self.scope[combo[j]]]['values']['rating']),
                     'to': float(values[j])}
                    for j in range(len(combo))
                ],
            }

    # -- exhaustive pass --------------------------------------------------
    def run(self, budget, deadline=None):
        import time
        used = 0
        while self.depth <= self.max_depth:
            if used >= budget or (deadline is not None and time.monotonic() > deadline):
                break
            if self.depth == 0:
                ranked = self.model.rank({})
                self.checked[0] = 1
                self.nodes += 1
                used += 1
                self.exhausted_depth = 0
                self.depth = 1
                self.cursor = None
                self.lower_bound = 1
                if self.best and self.best['size'] <= 1:
                    break
                continue
            if self.depth >= len(self.counts) or self.counts[self.depth] == 0:
                self.exhausted_depth = max(self.exhausted_depth, self.depth)
                self.depth += 1
                self.cursor = None
                self.lower_bound = self.exhausted_depth + 1
                continue
            finished = True
            for combo, value_index, values in self._plans(self.depth, self.cursor):
                if used >= budget or (deadline is not None and time.monotonic() > deadline):
                    self.cursor = {'indices': list(combo), 'value': value_index}
                    self.lower_bound = self.exhausted_depth + 1
                    return used
                edits = self._edits(combo, values)
                self.nodes += 1
                used += 1
                ranked = self.model.rank(edits)
                exact = self._confirm(edits, ranked)
                self.checked[self.depth] = self.checked.get(self.depth, 0) + 1
                if exact:
                    plan = self.make_plan(edits, exact)
                    self._keep(plan, 'exhaustive')
                    # every smaller depth is already fully checked here
                    self.exhausted_depth = max(self.exhausted_depth, self.depth - 1)
                    self.lower_bound = self.exhausted_depth + 1
                    self.cursor = {'indices': list(combo), 'value': value_index + 1}
                    finished = False
                    break
                self._note_near(combo, values, ranked)
            if not finished:
                return used
            self.exhausted_depth = max(self.exhausted_depth, self.depth)
            self.depth += 1
            self.cursor = None
            self.lower_bound = self.exhausted_depth + 1
            if self.best and self.best['size'] <= self.lower_bound:
                break
        return used

    # -- guided plan finder ----------------------------------------------
    def guided(self, budget, width=20, beam=6, max_size=6):
        """Heuristic plan finder; never lowers the proven bound.

        It tries, for each plan size, the ratings that move predictions the
        most.  The objective is how close the base leader is to being overtaken
        by *any* rival, so a new leader that is not the runner-up still counts.
        Every hit is re-checked by the production predictor before being kept.
        Returns the number of model evaluations spent; the caller adds them to
        ``nodes`` only when it also commits ``guided_done``, so an interrupted
        attempt never counts the same exploration twice.
        """
        used = 0
        base_top = self._base_top()
        pool = list(self.order)
        ranked_base = self.model.rank({})
        frontier = [({}, self._goal(ranked_base))]
        used += 1
        for size in range(1, min(self.max_depth, max_size) + 1):
            if used >= budget:
                return used
            candidates = []
            for current, _ in frontier:
                remaining = [i for i in pool if i not in current][:width]
                for index in remaining:
                    for value in self.options[index]:
                        if used >= budget:
                            return used
                        trial = dict(current)
                        trial[index] = value
                        ranked = self.model.rank(trial)
                        used += 1
                        goal = self._goal(ranked, base_top)
                        candidates.append((goal, trial))
                        if goal > 0 and size == len(trial):
                            exact = self._confirm(trial, ranked)
                            if exact:
                                plan = self.make_plan(trial, exact)
                                self._keep(plan, 'guided')
                                return used
            if not candidates:
                return used
            candidates.sort(key=lambda x: -x[0])
            frontier = [(trial, goal) for goal, trial in candidates[:beam]]
        return used

    def _goal(self, ranked_or_edits, base_top=None):
        if base_top is None:
            base_top = self._base_top()
        ranked = ranked_or_edits if isinstance(ranked_or_edits, list) else self.model.rank(ranked_or_edits)
        if not ranked:
            return -9.0
        values = {e: p for e, p, _ in ranked}
        top = values.get(base_top, -9.0)
        rivals = [p for e, p in values.items() if e != base_top]
        return (max(rivals) - top) if rivals else -9.0

    # -- reporting --------------------------------------------------------
    def status(self):
        if self.best and self.best['size'] <= self.lower_bound:
            return 'completed'
        if self.exhausted_depth >= self.max_depth:
            return 'none_found'
        if self.depth > self.max_depth:
            return 'none_found'
        return 'paused'

    def proof(self):
        """Plain-language statement about the current bound."""
        if self.status() == 'completed':
            return '已核对 0 至 %d 条改动的全部组合，%d 条改动最少，方案可复算。' % (
                self.lower_bound - 1, self.best['size'])
        if self.status() == 'none_found':
            return ('已核对待查范围内 0 至 %d 条改动的全部组合，不存在换掉首推的方案。'
                    % self.exhausted_depth)
        return '已核对 0 至 %d 条改动的全部组合；更少改动可以换掉首推的方案不存在。当前方案未证最小。' % (
            self.lower_bound - 1)

    def state(self):
        return {
            'depth': self.depth,
            'cursor': self.cursor,
            'checked': {str(k): v for k, v in sorted(self.checked.items())},
            'exhausted_depth': self.exhausted_depth,
            'best': self.best,
            'nodes': self.nodes,
            'lower_bound': self.lower_bound,
            'near_misses': self.near_misses,
            'guided_used': self.guided_used,
            'guided_targets': self.guided_targets,
            'guided_done': self.guided_done,
            'max_depth': self.max_depth,
            'option_counts': self.option_counts,
            'order': list(self.order),
        }


def _edited_checksum(users, edits, records):
    payload = {
        'ratings': sorted([u, e, round(float(v), 10)] for u, items in users.items() for e, v in items.items()),
        'edits': sorted([records[i]['reference'], round(float(v), 10)] for i, v in edits.items()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify(records, visitor, parameters, result, state):
    """Recompute a stored conclusion with the production predictor.

    Returns independent checks; the original run snapshot is never touched.
    """
    users = fit_ratings([r['values'] for r in records])
    by_ref = {r['reference']: r for r in records}
    checks = []
    base = ranking_of(users, visitor, parameters['neighbors'], parameters['min_common'])
    base_top = result['base_top'] if result else (base[0][0] if base else None)
    checks.append({
        'key': 'base_top',
        'label': '基准首推一致',
        'ok': bool(base) and base[0][0] == base_top,
        'expected': base_top,
        'actual': base[0][0] if base else '—',
    })

    if result:
        edited = {u: dict(items) for u, items in users.items()}
        problems = []
        for edit in result['edits']:
            row = by_ref.get(edit['reference'])
            if not row:
                problems.append(edit['reference'] + ' 不在快照中')
                continue
            values = row['values']
            if values['visitor'] != edit['visitor'] or values['exhibit'] != edit['exhibit']:
                problems.append(edit['reference'] + ' 的访客或展品不符')
                continue
            old, new = float(values['rating']), float(edit['to'])
            if abs(old - float(edit['from'])) > 1e-9:
                problems.append(edit['reference'] + ' 的原评分不符')
                continue
            if abs(new - old) > 1.0 + 1e-9 or not 1.0 <= new <= 5.0:
                problems.append(edit['reference'] + ' 超出最多一星的改动或越界')
                continue
            edited[values['visitor']][values['exhibit']] = new
        checks.append({
            'key': 'edits',
            'label': '每条改动合规',
            'ok': not problems,
            'actual': '；'.join(problems) if problems else '%d 条均为一星以内改动' % len(result['edits']),
        })
        after = ranking_of(edited, visitor, parameters['neighbors'], parameters['min_common'])
        checks.append({
            'key': 'flip',
            'label': '复算后首推更换',
            'ok': bool(after) and after[0][0] != (base[0][0] if base else None),
            'expected': result['after_top'],
            'actual': after[0][0] if after else '—',
        })
    else:
        checks.append({'key': 'edits', 'label': '每条改动合规', 'ok': True, 'actual': '尚无方案'})
        checks.append({'key': 'flip', 'label': '复算后首推更换', 'ok': True, 'actual': '尚无方案'})

    counts = combinations_at_most(state.get('option_counts') or [])
    checked = {int(k): int(v) for k, v in (state.get('checked') or {}).items()}
    exhaustive_prefix = int(state.get('exhausted_depth', -1)) + 1
    covering = all(checked.get(d, 0) == counts[d] for d in range(exhaustive_prefix) if d < len(counts))
    checks.append({
        'key': 'coverage',
        'label': '已查分支数与组合数一致',
        'ok': covering,
        'actual': '；'.join('%d 条改动 %d/%d' % (d, checked.get(d, 0), counts[d])
                           for d in range(0, min(exhaustive_prefix + 1, len(counts)))),
    })
    bound = int(state.get('lower_bound', 0))
    checks.append({
        'key': 'bound',
        'label': '可证下界与已穷尽层一致',
        'ok': exhaustive_prefix == bound and covering,
        'actual': '下界 %d 条改动' % bound,
    })
    return checks
