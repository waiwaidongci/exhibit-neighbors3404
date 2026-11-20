import json
import pytest
from fastapi.testclient import TestClient

import src.config.settings as settings
import src.config.database as database
import src.services.runs as runs_service
import src.services.counterfactual_jobs as jobs
from src.config.database import Base, engine, Session
from src.main import app
from src.models.user import User
from src.utils.security import hash_password


class FakeQueue:
    def __init__(self):
        self.items = []

    def rpush(self, name, value):
        self.items.append((name, value))
        return 1

    def ping(self):
        return True


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(engine)
    queue = FakeQueue()
    monkeypatch.setattr(runs_service, 'queue', queue)
    monkeypatch.setattr(jobs, 'queue', queue)
    with Session() as db:
        if not db.query(User).filter_by(email='admin@exhibit-neighbors.demo').first():
            db.add(User(email='admin@exhibit-neighbors.demo',
                        password_hash=hash_password('Research2026!'), role='admin'))
            db.add(User(email='viewer@exhibit-neighbors.demo',
                        password_hash=hash_password('Research2026!'), role='viewer'))
            db.commit()
    with TestClient(app) as c:
        c.queue = queue
        yield c


def login(client, email='admin@exhibit-neighbors.demo'):
    r = client.post('/auth/login', json={'email': email, 'password': 'Research2026!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def make_run(client, headers, count=240):
    import random
    rng = random.Random(42)
    source = client.post('/sources', headers=headers,
                         json={'name': '测试来源', 'filename': 'test.csv', 'description': '反事实复核测试'}).json()
    records = []
    for index in range(count):
        uid, slot = index // 6, index % 6
        exhibit = (uid % 3 * 3 + slot) % 12
        rating = round(max(1, min(5, 3 + (1 if exhibit % 3 == uid % 3 else -.4) + rng.gauss(0, .55))), 1)
        records.append({'reference': 'T-%04d' % (index + 1),
                        'values': {'visitor': 'V%03d' % (uid + 1),
                                   'exhibit': 'E%02d' % (exhibit + 1), 'rating': rating}})
    client.post('/sources/%d/records' % source['id'], headers=headers, json={'records': records})
    run = client.post('/runs', headers=headers,
                      json={'source_id': source['id'], 'name': '反事实基础研究',
                            'parameters': {'neighbors': 5, 'min_common': 2}}).json()
    job = client.queue.items[-1]
    assert job[0] == 'analysis:queue'
    from src.worker import execute
    execute(int(job[1]))
    detail = client.get('/runs/%d' % run['id'], headers=headers).json()
    assert detail['state'] == 'completed', detail.get('error')
    return detail


def process(client, job_id):
    from src.worker import run_counterfactual
    run_counterfactual(job_id)


def test_counterfactual_flow(client):
    headers = login(client)
    run = make_run(client, headers)
    options = client.get('/runs/%d/counterfactual/options' % run['id'], headers=headers).json()
    assert options['record_count'] == 240
    visitor = options['visitors'][0]['visitor']

    created = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                          json={'visitor': visitor, 'scope_mode': 'source', 'budget': 50000})
    assert created.status_code == 200, created.text
    job = created.json()
    assert job['scope_count'] == 240
    assert client.queue.items[-1][0] == 'counterfactual:queue'

    process(client, job['id'])
    detail = client.get('/counterfactual/%d' % job['id'], headers=headers).json()
    assert detail['status'] in ('completed', 'none_found')
    if detail['status'] == 'completed':
        assert detail['best']['size'] == detail['lower_bound']
        assert detail['proof']
    verify = client.post('/counterfactual/%d/verify' % job['id'], headers=headers).json()
    assert verify['all_ok'], verify

    # the original study is untouched
    after = client.get('/runs/%d' % run['id'], headers=headers).json()
    assert after['result']['checksum'] == run['result']['checksum']
    assert after['snapshot'] == run['snapshot']


def test_counterfactual_scope_and_dedup(client):
    headers = login(client)
    run = make_run(client, headers)
    options = client.get('/runs/%d/counterfactual/options' % run['id'], headers=headers).json()
    visitor = options['visitors'][0]['visitor']
    first = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                        json={'visitor': visitor, 'scope_mode': 'custom',
                              'scope_refs': ['T-0001', 'T-0002'], 'budget': 10000}).json()
    assert first['scope_count'] == 2
    process(client, first['id'])
    done = client.get('/counterfactual/%d' % first['id'], headers=headers).json()
    assert done['status'] in ('completed', 'none_found')
    again = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                        json={'visitor': visitor, 'scope_mode': 'custom',
                              'scope_refs': ['T-0002', 'T-0001'], 'budget': 10000}).json()
    assert again['id'] == first['id']

    missing = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                          json={'visitor': visitor, 'scope_refs': ['NOPE'], 'budget': 10000})
    assert missing.status_code == 422
    bad_visitor = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                              json={'visitor': 'ghost', 'budget': 10000})
    assert bad_visitor.status_code == 422


def test_viewer_cannot_start_but_can_read(client):
    admin = login(client)
    run = make_run(client, admin)
    options = client.get('/runs/%d/counterfactual/options' % run['id'], headers=admin).json()
    viewer = login(client, 'viewer@exhibit-neighbors.demo')
    job = client.post('/runs/%d/counterfactual' % run['id'], headers=admin,
                      json={'visitor': options['visitors'][0]['visitor'], 'budget': 5000}).json()
    process(client, job['id'])
    assert client.get('/counterfactual/%d' % job['id'], headers=viewer).status_code == 200
    denied = client.post('/runs/%d/counterfactual' % run['id'], headers=viewer,
                         json={'visitor': options['visitors'][0]['visitor'], 'budget': 5000})
    assert denied.status_code == 403


def test_resume_after_budget_exhaustion(client, monkeypatch):
    headers = login(client)
    run = make_run(client, headers)
    options = client.get('/runs/%d/counterfactual/options' % run['id'], headers=headers).json()
    monkeypatch.setattr(jobs, 'CHUNK', 200)
    monkeypatch.setattr(jobs.Search, 'guided', lambda self, budget, **kw: 0)
    created = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                          json={'visitor': options['visitors'][0]['visitor'], 'budget': 1000}).json()
    from src.models.counterfactual import CounterfactualSearch
    with Session() as db:
        db.get(CounterfactualSearch, created['id']).budget = 100   # tight budget for the test
        db.commit()
    process(client, created['id'])
    first = client.get('/counterfactual/%d' % created['id'], headers=headers).json()
    assert first['status'] == 'paused'
    assert first['lower_bound'] >= 1
    assert first['best'] is None or first['best']['size'] >= first['lower_bound']
    nodes_before = first['nodes']

    assert first['checked']['1'] < first['total_plans'][1]
    resumed = client.post('/counterfactual/%d/resume' % created['id'], headers=headers,
                          json={'budget': 200000})
    assert resumed.status_code == 200
    monkeypatch.setattr(jobs.Search, 'guided', lambda self, budget, **kw: 0)
    process(client, created['id'])
    final = client.get('/counterfactual/%d' % created['id'], headers=headers).json()
    assert final['status'] in ('completed', 'none_found')
    assert final['nodes'] > nodes_before
    if final['status'] == 'completed':
        assert final['best']['size'] == final['lower_bound']
        assert final['checked']['0'] == 1
        if final['lower_bound'] > 1:
            assert final['checked']['1'] == final['total_plans'][1]
    verify = client.post('/counterfactual/%d/verify' % created['id'], headers=headers).json()
    assert verify['all_ok']
    again = client.post('/counterfactual/%d/resume' % created['id'], headers=headers, json={})
    assert again.status_code == 409


def test_retry_does_not_double_count(client, monkeypatch):
    headers = login(client)
    run = make_run(client, headers)
    options = client.get('/runs/%d/counterfactual/options' % run['id'], headers=headers).json()
    monkeypatch.setattr(jobs, 'CHUNK', 50)
    job = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                      json={'visitor': options['visitors'][0]['visitor'], 'budget': 100000}).json()
    from src.worker import run_counterfactual
    import src.services.counterfactual_jobs as mod
    original = mod.Search.run

    calls = {'n': 0}

    def boom(self, budget, deadline=None):
        calls['n'] += 1
        used = original(self, budget, deadline)
        if calls['n'] == 2:
            raise RuntimeError('模拟worker中断')
        return used

    monkeypatch.setattr(mod.Search, 'run', boom)
    run_counterfactual(job['id'])
    broken = client.get('/counterfactual/%d' % job['id'], headers=headers).json()
    assert broken['status'] == 'failed'
    checked_before = broken['checked']
    nodes_before = broken['nodes']
    assert sum(checked_before.values()) > 0

    monkeypatch.setattr(mod.Search, 'run', original)
    client.post('/counterfactual/%d/resume' % job['id'], headers=headers, json={})
    run_counterfactual(job['id'])
    final = client.get('/counterfactual/%d' % job['id'], headers=headers).json()
    assert final['status'] in ('completed', 'none_found')
    assert final['nodes'] >= nodes_before
    for depth, count in final['checked'].items():
        assert count <= final['total_plans'][int(depth)]
    resumed_nodes = sum(final['checked'].values())
    assert resumed_nodes <= final['nodes']
    if final['status'] == 'completed' and final['lower_bound'] > 1:
        assert final['checked']['1'] == final['total_plans'][1]


def test_verify_detects_tampered_version(client):
    headers = login(client)
    run = make_run(client, headers)
    options = client.get('/runs/%d/counterfactual/options' % run['id'], headers=headers).json()
    job = client.post('/runs/%d/counterfactual' % run['id'], headers=headers,
                      json={'visitor': options['visitors'][0]['visitor'], 'budget': 200000}).json()
    process(client, job['id'])
    ok = client.post('/counterfactual/%d/verify' % job['id'], headers=headers).json()
    assert ok['all_ok']
    # simulate that the run snapshot no longer matches the searched version
    from src.models.run import Run
    with Session() as db:
        row = db.get(Run, run['id'])
        row.result = dict(row.result, checksum='deadbeef')
        db.commit()
    bad = client.post('/counterfactual/%d/verify' % job['id'], headers=headers).json()
    assert not bad['all_ok']
    assert any(c['key'] == 'version' and not c['ok'] for c in bad['checks'])
