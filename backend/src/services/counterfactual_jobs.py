"""Persistence and execution around the counterfactual search.

The original study is never modified: every search reads the frozen snapshot of
a completed run and stores its own scope, bound, plan and progress.  A search
job is identified by a fingerprint over the run checksum, the visitor, the
parameters and the exact ratings in scope, so resuming always continues the
same rating version.
"""
import json
from sqlalchemy import select
from fastapi import HTTPException
from redis import Redis

from ..config.settings import REDIS_URL
from ..models.run import Run
from ..models.counterfactual import CounterfactualSearch
from ..utils.serialization import row, now
from .audit import log
from .counterfactual import (Search, Model, influence_order, scope_fingerprint,
                             combinations_at_most, options_for, verify as verify_plan,
                             ranking_of)
from .runs import get_run

queue = Redis.from_url(REDIS_URL)

CHUNK = 2000
DEFAULT_BUDGET = 200000
MIN_BUDGET = 100
MAX_BUDGET = 5000000
ACTIVE = ('queued', 'running')


def _snapshot_records(run):
    if run.state != 'completed' or not run.result:
        raise HTTPException(409, '只可对已完成的研究发起反事实复核')
    return run.snapshot['records']


def _scope(run, refs):
    records = _snapshot_records(run)
    by_ref = {r['reference']: r for r in records}
    if not refs:
        chosen = [r['reference'] for r in records]
    else:
        missing = [ref for ref in refs if ref not in by_ref]
        if missing:
            raise HTTPException(422, '研究快照中没有这些样本编号：' + '、'.join(missing[:5]))
        wanted = set(refs)
        chosen = [r['reference'] for r in records if r['reference'] in wanted]
    if not chosen:
        raise HTTPException(422, '待查评分范围不能为空')
    return records, chosen


def _visitor(records, visitor):
    if not any(r['values']['visitor'] == visitor for r in records):
        raise HTTPException(422, '待查访客不在该研究快照中')
    rated = [r for r in records if r['values']['visitor'] == visitor]
    if not rated:
        raise HTTPException(422, '待查访客没有任何评分')
    return visitor


def _fingerprint(run, visitor, records, refs):
    return scope_fingerprint(run.result['checksum'], visitor, run.parameters, records, refs)


def _summary(job):
    data = {k: v for k, v in row(job).items() if k not in ('state', 'result')}
    state = job.state or {}
    result = job.result or {}
    data['scope_count'] = len(job.scope_refs or [])
    data['nodes'] = state.get('nodes', 0)
    data['lower_bound'] = state.get('lower_bound', 0)
    stored = result.get('best') or state.get('best') or {}
    data['best_size'] = stored.get('size')
    data['option_counts'] = state.get('option_counts') or []
    return data


def list_jobs(db, run_id=None):
    statement = select(CounterfactualSearch).order_by(CounterfactualSearch.id.desc())
    if run_id is not None:
        statement = statement.where(CounterfactualSearch.run_id == run_id)
    return [_summary(j) for j in db.scalars(statement.limit(100))]


def get_job(db, id):
    job = db.get(CounterfactualSearch, id)
    if not job:
        raise HTTPException(404, '未找到反事实复核任务')
    return job


def _enqueue(id):
    try:
        queue.rpush('counterfactual:queue', id)
    except Exception:
        raise HTTPException(503, '队列暂不可用，请稍后重试')


def create_job(db, user, data):
    budget = int(data.budget or DEFAULT_BUDGET)
    if not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise HTTPException(422, '搜索预算需在100至5000000之间')
    run = get_run(db, data.run_id)
    records, refs = _scope(run, data.scope_refs)
    visitor = _visitor(records, data.visitor)
    model = Model(records, visitor, run.parameters['neighbors'], run.parameters['min_common'])
    fingerprint = _fingerprint(run, visitor, records, refs)

    existing = db.scalar(select(CounterfactualSearch)
                         .where(CounterfactualSearch.fingerprint == fingerprint)
                         .order_by(CounterfactualSearch.id.desc()))
    if existing:
        if existing.status in ACTIVE:
            return _summary(existing), False
        if existing.status in ('paused', 'failed'):
            existing.status = 'queued'
            existing.error = ''
            existing.budget = budget
            existing.updated_at = now()
            log(db, user, 'counterfactual.resumed', existing.id, existing.visitor)
            db.commit()
            _enqueue(existing.id)
            return _summary(existing), True
        return _summary(existing), False

    scope_set = set(refs)
    indices = [i for i, r in enumerate(records) if r['reference'] in scope_set]
    order = influence_order(model, records, indices)
    state = {
        'order': order,
        'depth': 0,
        'cursor': None,
        'checked': {},
        'exhausted_depth': -1,
        'best': None,
        'nodes': 0,
        'lower_bound': 0,
        'near_misses': {},
        'guided_used': 0,
        'guided_targets': [],
        'guided_done': False,
        'option_counts': [len(options_for(records[i]['values']['rating'])) for i in order],
    }
    job = CounterfactualSearch(
        run_id=run.id, visitor=visitor, scope_mode=(data.scope_mode or 'custom'),
        scope_refs=refs, parameters=run.parameters, fingerprint=fingerprint,
        status='queued', budget=budget, state=state, result=None,
        created_by=user.email, created_at=now(), updated_at=now())
    db.add(job)
    db.flush()
    log(db, user, 'counterfactual.created', job.id,
        '%s · %d 条待查 · 预算 %d' % (visitor, len(refs), budget))
    db.commit()
    _enqueue(job.id)
    return _summary(job), False


def resume_job(db, user, id, budget=None):
    job = get_job(db, id)
    if job.status in ACTIVE:
        return _summary(job)
    if job.status in ('completed', 'none_found'):
        raise HTTPException(409, '该复核已有完整结论，无需继续')
    if budget is not None:
        value = int(budget)
        if not MIN_BUDGET <= value <= MAX_BUDGET:
            raise HTTPException(422, '搜索预算需在100至5000000之间')
        job.budget = value
    job.status = 'queued'
    job.error = ''
    job.updated_at = now()
    log(db, user, 'counterfactual.resumed', job.id, job.visitor)
    db.commit()
    _enqueue(job.id)
    return _summary(job)


def detail(db, id):
    job = get_job(db, id)
    run = db.get(Run, job.run_id)
    records = run.snapshot['records']
    by_ref = {r['reference']: r for r in records}
    base = ranking_of(_users(records), job.visitor,
                      job.parameters['neighbors'], job.parameters['min_common'])
    state = job.state or {}
    result = job.result or {}
    counts = combinations_at_most(state.get('option_counts') or [])
    data = _summary(job)
    stored_best = result.get('best') or state.get('best')
    proof = result.get('proof') or _paused_proof(state)
    data.update({
        'run_name': run.name,
        'run_checksum': run.result['checksum'],
        'base_ranking': [{'exhibit': e, 'prediction': p, 'method': m} for e, p, m in base[:6]],
        'base_top': base[0][0] if base else None,
        'scope': [{'reference': ref, 'visitor': by_ref[ref]['values']['visitor'],
                   'exhibit': by_ref[ref]['values']['exhibit'],
                   'rating': by_ref[ref]['values']['rating']} for ref in job.scope_refs],
        'best': stored_best,
        'proof': proof,
        'checked': state.get('checked') or {},
        'exhausted_depth': state.get('exhausted_depth', -1),
        'near_misses': state.get('near_misses') or {},
        'guided_used': state.get('guided_used', 0),
        'total_plans': list(counts),
        'parameters': job.parameters,
    })
    return data


def _paused_proof(state):
    """Honest statement for a search that stopped before proving a bound."""
    exhausted = int(state.get('exhausted_depth', -1))
    best = state.get('best')
    prefix = ('已核对 0 至 %d 条改动的全部组合；更少改动可以换掉首推的方案不存在。'
              % exhausted if exhausted >= 0 else '尚未核对完 0 条改动的组合。')
    if best:
        if best['size'] <= exhausted + 1:
            return prefix + '%d 条改动最少，方案可复算。' % best['size']
        return prefix + '已找到 %d 条改动的方案，但该层尚未核对完毕，暂不视为最少。' % best['size']
    return prefix + '当前尚未找到方案，正在更大的改动条数中继续搜索。'


def _users(records):
    from .algorithm import fit_ratings
    return fit_ratings([r['values'] for r in records])


def _build_search(job, records):
    state = job.state or {}
    model = Model(records, job.visitor, job.parameters['neighbors'], job.parameters['min_common'])
    order = state.get('order')
    if order is None:
        raise ValueError('复核任务缺少待查顺序，请重新发起')
    return Search(model, records, order, state)


def execute(db, id):
    """Process one attempt in committed chunks.

    ``state['nodes']`` is only advanced together with ``state['checked']`` and
    the cursor, in the same commit.  A crash therefore rewinds to the last
    commit; the retried chunk recomputes the same plans without counting them
    twice.
    """
    # Claim the job under a row lock so two consumers cannot run it at once.
    job = db.get(CounterfactualSearch, id, with_for_update=True)
    if not job or job.status not in ('queued', 'running'):
        db.commit()
        return
    run = db.get(Run, job.run_id)
    job.status = 'running'
    job.attempts = (job.attempts or 0) + 1
    job.updated_at = now()
    db.commit()
    try:
        records = run.snapshot['records']
        search = _build_search(job, records)
        state = job.state or {}
        if not state.get('guided_done') and not search.best:
            guided_budget = max(400, min(2000, job.budget // 20))
            spent = search.guided(guided_budget)
            search.nodes += spent
            search.guided_used += spent
            search.guided_done = True
            job.state = search.state()
            job.updated_at = now()
            db.commit()
        while search.nodes < job.budget:
            search.run(min(CHUNK, job.budget - search.nodes))
            job.state = search.state()
            job.updated_at = now()
            db.commit()
            if search.status() in ('completed', 'none_found'):
                break
        status = search.status()
        job.state = search.state()
        job.updated_at = now()
        if status == 'paused':
            job.status = 'paused'
            db.commit()
            log(db, 'worker', 'counterfactual.paused', job.id,
                '下界 %d · 已查 %d 个方案' % (search.lower_bound, search.nodes))
            db.commit()
            return
        job.result = {
            'visitor': job.visitor,
            'edited_checksum': (search.best or {}).get('edited_checksum'),
            'base_top': search.base_ranking()[0][0],
            'base_ranking': [{'exhibit': e, 'prediction': p, 'method': m}
                             for e, p, m in search.base_ranking()[:6]],
            'best': search.best,
            'lower_bound': search.lower_bound,
            'proof': search.proof(),
            'status': status,
            'nodes': search.nodes,
            'checked': {str(k): v for k, v in sorted(search.checked.items())},
            'option_counts': search.option_counts,
            'scope_count': len(job.scope_refs),
            'fingerprint': job.fingerprint,
            'version_checksum': run.result['checksum'],
        }
        job.status = status
        db.commit()
        log(db, 'worker', 'counterfactual.completed', job.id,
            '%s · %s' % (status, search.proof()))
        db.commit()
    except Exception as e:
        import logging
        logging.exception('counterfactual failed')
        db.rollback()
        job = db.get(CounterfactualSearch, id)
        if not job:
            return
        # A finished conclusion is never overwritten by a later failure.
        if job.status in ('completed', 'none_found'):
            return
        job.status = 'failed'
        job.error = str(e)[:2000]
        job.updated_at = now()
        db.commit()
        log(db, 'worker', 'counterfactual.failed', id, job.error)


def options(db, id):
    run = get_run(db, id)
    records = _snapshot_records(run)
    if not run.result:
        raise HTTPException(409, '该研究尚未生成结果，无法发起复核')
    visitors = {}
    for r in records:
        v = r['values']['visitor']
        visitors[v] = visitors.get(v, 0) + 1
    rateable = sum(1 for r in records if len(options_for(r['values']['rating'])) > 0)
    try:
        model = Model(records, sorted(visitors)[0] if visitors else '', run.parameters['neighbors'], run.parameters['min_common'])
        base_ranking = [{'exhibit': e, 'prediction': p, 'method': m} for e, p, m in model.base_rank[:5]]
        base_top = model.base_rank[0][0] if model.base_rank else None
    except ValueError:
        base_ranking, base_top = [], None
    return {
        'run_id': run.id,
        'name': run.name,
        'visitors': [{'visitor': v, 'ratings': c} for v, c in sorted(visitors.items())],
        'record_count': len(records),
        'rateable': rateable,
        'parameters': run.parameters,
        'checksum': run.result['checksum'],
        'sample_base_ranking': base_ranking,
        'sample_visitor': sorted(visitors)[0] if visitors else None,
        'sample_base_top': base_top,
    }


def verification(db, id):
    job = get_job(db, id)
    run = db.get(Run, job.run_id)
    records = run.snapshot['records']
    state = job.state or {}
    plan = (job.result or {}).get('best') or state.get('best')
    checks = verify_plan(records, job.visitor, job.parameters, plan, state)
    current = scope_fingerprint(run.result['checksum'], job.visitor, job.parameters, records, job.scope_refs)
    checks.insert(0, {
        'key': 'version',
        'label': '评分版本与待查范围一致',
        'ok': current == job.fingerprint,
        'actual': '指纹 ' + job.fingerprint[:12] + '…',
        'expected': '指纹 ' + current[:12] + '…',
    })
    return {'job_id': job.id, 'status': job.status,
            'proof': (job.result or {}).get('proof') or _paused_proof(state),
            'provisional': job.status not in ('completed', 'none_found'),
            'checks': checks,
            'all_ok': all(c['ok'] for c in checks)}
