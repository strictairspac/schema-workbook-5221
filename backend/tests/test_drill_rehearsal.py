"""Data-migration rehearsal contract tests.

Run from ``backend``: ``python -m pytest tests -q``. Uses in-memory SQLite and a fake Redis;
the MySQL-specific row locks degrade to no-ops, the unique assignment key still enforces the
single-effective-plan rule.
"""
import os,sys
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DATABASE_URL','sqlite+pysqlite://')

import pytest
from sqlalchemy import create_engine,update
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

import src.config.database as database
engine=create_engine('sqlite+pysqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
database.engine=engine;database.Session.configure(bind=engine)
import src.services.reports as reports_service
import fakeredis
reports_service.queue=fakeredis.FakeRedis(decode_responses=True)

from src.main import app
from src.models.entities import Base,User,Relation,Dependency,Report
from src.services.normalization import analyze
from src.utils.security import password_hash
import src.services.drills as drills

ATTRS=['委托号','客户','检测项','方法','结果']
FDS=[{'lhs':['委托号'],'rhs':['客户']},{'lhs':['检测项'],'rhs':['方法']},{'lhs':['委托号','检测项'],'rhs':['结果']}]

def drop_physical_tables():
    from sqlalchemy import MetaData,inspect
    insp=inspect(engine)
    with engine.begin() as conn:
        for name in insp.get_table_names():
            if name.startswith('drill_src_') or name.startswith('drill_t_'):
                metadata=MetaData();metadata.reflect(bind=conn,only=[name]);metadata.tables[name].drop(conn)

@pytest.fixture()
def env():
    Base.metadata.drop_all(engine)
    drop_physical_tables()
    Base.metadata.create_all(engine)
    with database.Session() as db:
        for role in ('admin','operator','viewer'):
            db.add(User(email=role+'@nanlu.test',password=password_hash('Demo2026!'),role=role))
        rel=Relation(code='NL-1117-A',title='材料检测委托单',attributes=ATTRS);db.add(rel);db.flush()
        for fd in FDS:db.add(Dependency(relation_id=rel.id,lhs=fd['lhs'],rhs=fd['rhs']))
        report=Report(name='shift-test',relation_id=rel.id,status='completed',
                      input_snapshot={'relation':{'attributes':ATTRS},'dependencies':FDS},result=analyze(ATTRS,FDS))
        db.add(report);db.commit()
        rel_id,report_id=rel.id,report.id
    client=TestClient(app)
    token=client.post('/auth/login',json={'email':'operator@nanlu.test','password':'Demo2026!'}).json()['token']
    return {'client':client,'headers':{'Authorization':'Bearer '+token},'relation_id':rel_id,'report_id':report_id}

def make_drill(env,name='演练'):
    res=env['client'].post('/drills',headers=env['headers'],json={'name':name,'report_id':env['report_id']})
    assert res.status_code==201,res.text
    return res.json()

def add(env,did,**kw):
    values={'委托号':'W1','客户':'C1','检测项':'I1','方法':'M1','结果':'R1'};values.update(kw)
    return env['client'].post(f'/drills/{did}/rows',headers=env['headers'],json={'values':values})

def test_builds_real_tables_from_report_and_switches(env):
    drill=make_drill(env);did=drill['id']
    tables=env['client'].get(f'/drills/{did}/tables',headers=env['headers']).json()
    assert tables['source_table']==f'drill_src_{env["relation_id"]}'
    assert len(tables['new_tables'])==3
    assert add(env,did).status_code==201
    assert add(env,did,委托号='W2',检测项='I2',方法='M2',结果='R2').status_code==201
    progress=env['client'].post(f'/drills/{did}/migrate',headers=env['headers']).json()['progress']
    assert progress['done'] and progress['copied']==2
    cmp=env['client'].get(f'/drills/{did}/compare',headers=env['headers']).json()
    assert cmp['equal'] and cmp['source_count']==2==cmp['new_count']
    switched=env['client'].post(f'/drills/{did}/switch',headers=env['headers']).json()
    assert switched['status']=='switched'
    rows=env['client'].get(f'/drills/{did}/rows?view=current',headers=env['headers']).json()
    assert rows['view']=='new' and len(rows['rows'])==2
    assert add(env,did,委托号='W3',检测项='I3',方法='M3',结果='R3').status_code==201
    assert len(env['client'].get(f'/drills/{did}/rows?view=source',headers=env['headers']).json()['rows'])==2
    assert len(env['client'].get(f'/drills/{did}/rows?view=new',headers=env['headers']).json()['rows'])==3

def test_edit_during_migration_is_carried_and_conflicts_rejected(env):
    drill=make_drill(env);did=drill['id']
    assert add(env,did).status_code==201
    env['client'].post(f'/drills/{did}/migrate',headers=env['headers'])
    assert add(env,did,委托号='W2',检测项='I2',方法='M2',结果='R2').status_code==201
    res=add(env,did,委托号='W3',检测项='I1',方法='M9')
    assert res.status_code==409 and '检测项' in res.json()['detail'] and '方法' in res.json()['detail']
    assert len(env['client'].get(f'/drills/{did}/rows?view=source',headers=env['headers']).json()['rows'])==2
    env['client'].post(f'/drills/{did}/migrate',headers=env['headers'])
    assert env['client'].get(f'/drills/{did}/compare',headers=env['headers']).json()['equal']
    changes=env['client'].get(f'/drills/{did}/changes',headers=env['headers']).json()
    assert any(c['action']=='create' for c in changes)

def test_cascade_correction_keeps_shared_dependency(env):
    drill=make_drill(env);did=drill['id']
    add(env,did)
    add(env,did,委托号='W2',结果='R2')
    res=env['client'].put(f'/drills/{did}/rows/2',headers=env['headers'],json={'values':{'委托号':'W2','客户':'C2','检测项':'I1','方法':'M2','结果':'R2'}})
    assert res.status_code==409
    res=env['client'].put(f'/drills/{did}/rows/2',headers=env['headers'],json={'values':{'委托号':'W2','客户':'C2','检测项':'I1','方法':'M2','结果':'R2'},'cascade':True})
    assert res.status_code==200,res.text
    rows=env['client'].get(f'/drills/{did}/rows?view=source',headers=env['headers']).json()['rows']
    assert {r['values']['方法'] for r in rows}=={'M2'}

def test_interrupted_migration_resumes_without_duplicates(env):
    drill=make_drill(env);did=drill['id']
    for i in range(3):add(env,did,委托号=f'W{i}',检测项=f'I{i}',方法=f'M{i}',结果=f'R{i}')
    first=env['client'].post(f'/drills/{did}/migrate?max_rows=1',headers=env['headers']).json()['progress']
    assert first['copied']==1 and not first['done']
    second=env['client'].post(f'/drills/{did}/migrate?max_rows=1',headers=env['headers']).json()['progress']
    assert second['copied']==2 and not second['done']
    final=env['client'].post(f'/drills/{did}/migrate?max_rows=1',headers=env['headers']).json()['progress']
    again=env['client'].post(f'/drills/{did}/migrate',headers=env['headers']).json()['progress']
    assert final['done'] and again['copied']==3
    assert len(env['client'].get(f'/drills/{did}/rows?view=new',headers=env['headers']).json()['rows'])==3
    assert env['client'].get(f'/drills/{did}/compare',headers=env['headers']).json()['equal']

def test_only_one_plan_takes_effect_and_original_table_survives(env):
    first=make_drill(env,'方案A');second=make_drill(env,'方案B')
    add(env,first['id']);add(env,second['id'],客户='C2')
    for d in (first,second):env['client'].post(f"/drills/{d['id']}/migrate",headers=env['headers'])
    assert env['client'].post(f"/drills/{first['id']}/switch",headers=env['headers']).json()['status']=='switched'
    res=env['client'].post(f"/drills/{second['id']}/switch",headers=env['headers'])
    assert res.status_code==409 and '原表' in res.json()['detail']
    assert env['client'].get(f"/drills/{second['id']}/rows?view=source",headers=env['headers']).json()['rows']
    res=env['client'].post(f"/drills/{second['id']}/rows",headers=env['headers'],json={'values':{'委托号':'W9','客户':'C9','检测项':'I9','方法':'M9','结果':'R9'}})
    assert res.status_code==409

def test_report_and_plan_frozen_at_creation(env):
    drill=make_drill(env);did=drill['id']
    with database.Session() as db:
        rel=db.get(Relation,env['relation_id']);rel.attributes=ATTRS+['备注']
        db.add(Dependency(relation_id=rel.id,lhs=['客户'],rhs=['结果']));db.commit()
    detail=env['client'].get(f'/drills/{did}',headers=env['headers']).json()
    assert detail['plan_summary']['source_attributes']==ATTRS
    assert detail['plan_summary']['dependencies']==FDS
    assert add(env,did).status_code==201
    assert env['client'].post(f'/drills/{did}/migrate',headers=env['headers']).json()['progress']['done']
    assert env['client'].get(f'/reports/{env["report_id"]}',headers=env['headers']).status_code==200

def test_switch_reconciles_writes_that_bypassed_sync(env):
    drill=make_drill(env);did=drill['id']
    add(env,did)
    env['client'].post(f'/drills/{did}/migrate',headers=env['headers'])
    source=drills._source(drill['plan'])
    with engine.begin() as conn:
        conn.execute(update(source).where(source.c._row_id==1).values(客户='C7'))
    assert env['client'].get(f'/drills/{did}/compare',headers=env['headers']).json()['equal'] is False
    assert env['client'].post(f'/drills/{did}/switch',headers=env['headers']).json()['status']=='switched'
    rows=env['client'].get(f'/drills/{did}/rows?view=new',headers=env['headers']).json()['rows']
    assert rows[0]['values']['客户']=='C7'
    assert env['client'].get(f'/drills/{did}/compare',headers=env['headers']).json()['equal']

def test_rejects_incomplete_or_unverified_reports(env):
    with database.Session() as db:
        rel=db.get(Relation,env['relation_id'])
        bad=Report(name='bad',relation_id=rel.id,status='completed',input_snapshot={'relation':{'attributes':ATTRS},'dependencies':FDS},
                   result={'all_3nf':False,'chase':{'lossless':True},'dependency_preserved':True,'decomposition':[ATTRS],'checksum':'x'})
        queued=Report(name='queued',relation_id=rel.id,status='queued',input_snapshot={'relation':{'attributes':ATTRS},'dependencies':FDS})
        db.add_all([bad,queued]);db.commit();bad_id,queued_id=bad.id,queued.id
    assert env['client'].post('/drills',headers=env['headers'],json={'name':'xx','report_id':bad_id}).status_code==409
    assert env['client'].post('/drills',headers=env['headers'],json={'name':'xx','report_id':queued_id}).status_code==409

def test_viewer_is_read_only(env):
    drill=make_drill(env);did=drill['id'];add(env,did)
    viewer=env['client'].post('/auth/login',json={'email':'viewer@nanlu.test','password':'Demo2026!'}).json()['token']
    headers={'Authorization':'Bearer '+viewer}
    assert env['client'].get('/drills',headers=headers).status_code==200
    assert env['client'].get(f'/drills/{did}/compare',headers=headers).status_code==200
    assert env['client'].post(f'/drills/{did}/migrate',headers=headers).status_code==403
    assert env['client'].post('/drills',headers=headers,json={'name':'xx','report_id':env['report_id']}).status_code==403

def test_switch_precheck_failure_keeps_original_table_and_can_retry(env):
    drill=make_drill(env);did=drill['id']
    add(env,did);add(env,did,委托号='W2',检测项='I2',方法='M2',结果='R2')
    env['client'].post(f'/drills/{did}/migrate',headers=env['headers'])
    # an out-of-band writer violates the frozen dependency after migration completed
    source=drills._source(drill['plan'])
    with engine.begin() as conn:
        conn.execute(update(source).where(source.c._row_id==2).values(方法='M9',检测项='I1'))
    res=env['client'].post(f'/drills/{did}/switch',headers=env['headers'])
    assert res.status_code==409 and '原表' in res.json()['detail']
    detail=env['client'].get(f'/drills/{did}',headers=env['headers']).json()
    assert detail['status']=='failed'
    rows=env['client'].get(f'/drills/{did}/rows?view=source',headers=env['headers']).json()['rows']
    assert len(rows)==2
    # correcting the conflicting row makes the drill retryable and switchable
    assert env['client'].put(f'/drills/{did}/rows/2',headers=env['headers'],json={'values':{'委托号':'W2','客户':'C1','检测项':'I1','方法':'M1','结果':'R2'}}).status_code==200
    assert env['client'].post(f'/drills/{did}/migrate',headers=env['headers']).json()['progress']['done']

def test_switch_before_migration_completes_is_rejected(env):
    drill=make_drill(env);did=drill['id']
    add(env,did);add(env,did,委托号='W2',检测项='I2',方法='M2',结果='R2')
    res=env['client'].post(f'/drills/{did}/switch',headers=env['headers'])
    assert res.status_code==409 and '迁移尚未完成' in res.json()['detail']
    assert env['client'].get(f'/drills/{did}',headers=env['headers']).json()['status']=='pending'
    assert len(env['client'].get(f'/drills/{did}/rows?view=source',headers=env['headers']).json()['rows'])==2

def test_failed_migration_keeps_original_and_retries_cleanly(env,monkeypatch):
    drill=make_drill(env);did=drill['id']
    for i in range(4):add(env,did,委托号=f'W{i}',检测项=f'I{i}',方法=f'M{i}',结果=f'R{i}')
    original=drills._upsert_row;calls={'n':0}
    def crash(conn,plan,key,values):
        calls['n']+=1
        if calls['n']>2:raise RuntimeError('模拟停机')
        return original(conn,plan,key,values)
    monkeypatch.setattr(drills,'_upsert_row',crash)
    res=env['client'].post(f'/drills/{did}/migrate',headers=env['headers'])
    assert res.status_code==500
    monkeypatch.setattr(drills,'_upsert_row',original)
    assert len(env['client'].get(f'/drills/{did}/rows?view=source',headers=env['headers']).json()['rows'])==4
    progress=env['client'].get(f'/drills/{did}',headers=env['headers']).json()['progress']
    # the interrupted batch rolled back together with its cursor; accepted source edits were
    # already mirrored by the live sync, so only the batch upserts are replayed on retry
    assert progress['copied']==0 and not progress['done']
    assert env['client'].post(f'/drills/{did}/migrate',headers=env['headers']).json()['progress']['done']
    rows=env['client'].get(f'/drills/{did}/rows?view=new',headers=env['headers']).json()['rows']
    assert len(rows)==4 and env['client'].get(f'/drills/{did}/compare',headers=env['headers']).json()['equal']
