"""Data-migration rehearsal: freeze a report, build real tables, backfill, verify and switch.

The original commission table (``drill_src_<relation>``) is never dropped. Target tables are
``drill_t_<drill>_<n>``; each target row carries ``_src_key`` so retries upsert rather than
duplicate. A relation-level lock row plus the unique ``drill_assignments.relation_id`` make the
switch serializable: exactly one plan can take effect for a commission table.
"""
import hashlib,json,uuid
from collections import Counter
from datetime import datetime,timezone
from sqlalchemy import (Column,Integer,MetaData,String,Table,Text,delete,inspect,
                        select,update)
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.config.database import engine
from src.constants.enums import DRILL_STATES
from src.models.entities import Audit,Drill,DrillAssignment,DrillChange,Relation,RelationLock,Report
from src.utils.serialization import record

INTERNAL_ATTRIBUTES={'_row_id','_src_key'}
MAX_ROWS_PER_CALL=500

def source_table_name(relation_id):return f'drill_src_{relation_id}'
def _assert_state(value):
    if value not in DRILL_STATES:raise ValueError('未知演练状态：'+value)
    return value
def target_table_name(drill_id,index):return f'drill_t_{drill_id}_{index}'

def _source_table(relation_id,attributes):
    return Table(source_table_name(relation_id),MetaData(),
        Column('_row_id',Integer,primary_key=True,autoincrement=True),
        *[Column(a,Text) for a in attributes])

def _target_table(name,attributes):
    return Table(name,MetaData(),Column('_src_key',String(64),primary_key=True),
        *[Column(a,Text) for a in attributes])

def _source(plan):return _source_table(plan['source_relation_id'],plan['source_attributes'])
def _targets(plan):return [_target_table(x['table'],x['attributes']) for x in plan['relations']]

def _columns(name):
    insp=inspect(engine)
    if not insp.has_table(name):return None
    return [c['name'] for c in insp.get_columns(name)]

def _create_tables(plan,force=False):
    names=[source_table_name(plan['source_relation_id'])]+[t['table'] for t in plan['relations']]
    if not force and all(_columns(name) is not None for name in names):return
    with engine.begin() as conn:
        _source(plan).create(conn,checkfirst=True)
        for t in _targets(plan):t.create(conn,checkfirst=True)
    cols=_columns(source_table_name(plan['source_relation_id']))
    expected=set(plan['source_attributes'])|{'_row_id'}
    if set(cols or [])!=expected:
        raise HTTPException(409,'原表 '+source_table_name(plan['source_relation_id'])
            +' 已存在且字段与冻结报告不一致：现有 '+', '.join(cols or [])+'；报告要求 '+', '.join(plan['source_attributes']))

def _plan_from_report(report):
    if not report:raise HTTPException(404,'报告不存在')
    if report.status!='completed' or not report.result:raise HTTPException(409,'报告尚未完成，不能开始拆表演练')
    result=report.result
    if not (result.get('all_3nf') and (result.get('chase') or {}).get('lossless') and result.get('dependency_preserved')):
        raise HTTPException(409,'报告未通过3NF、无损或依赖保持校验，不能作为拆表方案')
    snapshot=report.input_snapshot or {}
    relation=snapshot.get('relation') or {}
    attributes=list(relation.get('attributes') or [])
    dependencies=snapshot.get('dependencies') or []
    if not attributes:raise HTTPException(409,'报告快照缺少原表属性，不能演练')
    if INTERNAL_ATTRIBUTES & set(attributes):raise HTTPException(409,'原表属性不得使用内部保留名 _row_id/_src_key')
    decomposition=result.get('decomposition') or []
    if not decomposition:raise HTTPException(409,'报告没有可建表的分解关系')
    union=set()
    for attrs in decomposition:
        if not attrs or INTERNAL_ATTRIBUTES & set(attrs):raise HTTPException(409,'分解关系为空或使用内部保留属性名，不能建表')
        union.update(attrs)
    if union!=set(attributes):
        raise HTTPException(409,'分解关系属性并集与原表属性不一致，不能保证还原：缺少 '
            +', '.join(sorted(set(attributes)-union))+'；多出 '+', '.join(sorted(union-set(attributes))))
    relations=[{'name':f'R{i}','table':target_table_name(0,0),'attributes':list(attrs)} for i,attrs in enumerate(decomposition,start=1)]
    return {'source_relation_id':report.relation_id,'source_attributes':attributes,'dependencies':dependencies,
            'candidate_keys':result.get('candidate_keys') or [],'report_checksum':result.get('checksum'),
            'report_name':report.name,'relations':relations}

def _ensure_lock(db,relation_id):
    if db.get(RelationLock,relation_id):return
    if db.bind.dialect.name!='mysql':db.add(RelationLock(relation_id=relation_id));db.flush();return
    try:
        with db.begin_nested():
            db.add(RelationLock(relation_id=relation_id));db.flush()
    except IntegrityError:pass

def _lock_relation(db,relation_id):
    """Row lock serializing source writes, migration and switch for one commission table."""
    _ensure_lock(db,relation_id)
    db.execute(select(RelationLock).where(RelationLock.relation_id==relation_id).with_for_update())

def _detail(obj):
    data=record(obj);data['plan_summary']={'source_table':source_table_name(obj.relation_id),
        'source_attributes':obj.plan['source_attributes'],'relations':obj.plan['relations'],
        'dependencies':obj.plan['dependencies'],'candidate_keys':obj.plan['candidate_keys'],
        'report_name':obj.plan.get('report_name'),'report_checksum':obj.plan.get('report_checksum')}
    return data

def listing(db):return [_detail(x) for x in db.scalars(select(Drill).order_by(Drill.id.desc()))]

def detail(db,ident):
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    return _detail(obj)

def create(db,data,user):
    report=db.get(Report,data.report_id)
    plan=_plan_from_report(report)
    if not db.get(Relation,report.relation_id):raise HTTPException(404,'报告对应的关系草案不存在')
    _ensure_lock(db,report.relation_id)
    assignment=_assignment(db,report.relation_id,current=True)
    if assignment:raise HTTPException(409,f'该委托表已由演练 #{assignment.drill_id} 切换生效，不能再建新方案')
    obj=Drill(name=data.name,relation_id=report.relation_id,report_id=report.id,status=_assert_state('pending'),plan=plan,
              progress={'cursor':0,'copied':0,'total':0,'done':False})
    db.add(obj);db.flush()
    for index,rel in enumerate(obj.plan['relations']):rel['table']=target_table_name(obj.id,index)
    flag_modified(obj,'plan')
    db.commit()
    try:_create_tables(obj.plan)
    except HTTPException:
        obj.status=_assert_state('failed');obj.error='建表冲突，原表未改动';db.commit();raise
    except Exception as exc:
        obj.status=_assert_state('failed');obj.error='建表失败，原表未改动：'+str(exc);db.commit()
        raise HTTPException(409,obj.error)
    _ensure_lock(db,obj.relation_id)
    db.add(Audit(actor=user.email,action='创建拆表演练 '+data.name))
    db.commit()
    return _detail(obj)

def _active_drills(db,relation_id):
    """Drills whose journal should record accepted source corrections."""
    return list(db.scalars(select(Drill).where(Drill.relation_id==relation_id,Drill.status.in_(('pending','migrating','failed'))).order_by(Drill.id)))

def _assignment(db,relation_id,current=False):
    """Read the effective plan. ``current`` uses a locking read so a switch committed by a
    concurrent transaction is visible even under MySQL's REPEATABLE READ snapshot."""
    if not current:return db.get(DrillAssignment,relation_id)
    return db.execute(select(DrillAssignment).where(DrillAssignment.relation_id==relation_id).with_for_update()).scalar_one_or_none()

def _read_source(conn,plan,for_update=False):
    table=_source(plan)
    stmt=select(table).order_by(table.c._row_id)
    if for_update:stmt=stmt.with_for_update()
    return [{'key':row._mapping['_row_id'],'values':{a:row._mapping[a] for a in plan['source_attributes']}}
            for row in conn.execute(stmt)]

def _read_new(conn,plan):
    merged={}
    for rel in plan['relations']:
        table=_target_table(rel['table'],rel['attributes'])
        for row in conn.execute(select(table).order_by(table.c._src_key)):
            key=row._mapping['_src_key'];entry=merged.setdefault(key,{})
            for a in rel['attributes']:entry[a]=row._mapping[a]
    return [{'key':key,'values':merged[key]} for key in sorted(merged)]

def _read_source_safe(conn,plan):
    try:return _read_source(conn,plan)
    except Exception:return []

def _read_new_safe(conn,plan):
    try:return _read_new(conn,plan)
    except Exception:return []

def rows(db,ident,view='current'):
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    if view in ('source','new'):
        with engine.connect() as conn:
            return {'view':view,'rows':_read_source_safe(conn,obj.plan) if view=='source' else _read_new_safe(conn,obj.plan)}
    assignment=_assignment(db,obj.relation_id)
    with engine.connect() as conn:
        if assignment and assignment.drill_id==obj.id:return {'view':'new','rows':_read_new_safe(conn,obj.plan)}
        return {'view':'source','rows':_read_source_safe(conn,obj.plan)}

def _checksum(rows,attributes):
    tuples=sorted(tuple((row['values'].get(a) or '') for a in attributes) for row in rows)
    return hashlib.sha256(json.dumps(tuples,ensure_ascii=False).encode()).hexdigest(),tuples

def _label(key):return '待写入行' if key in (None,'','new',0) else f'行{key}'

def _conflicts(rows,dependencies):
    conflicts=[]
    for fd in dependencies:
        lhs=list(fd.get('lhs') or []);rhs=list(fd.get('rhs') or [])
        if not rhs:continue
        observed={}
        for row in rows:
            left=tuple((row['values'].get(a) or '') for a in lhs)
            for a in rhs:observed.setdefault((left,a),[]).append((row['key'],row['values'].get(a) or ''))
        for (left,a),values in observed.items():
            if len({v for _,v in values})>1:
                lhs_text='{ '+', '.join(lhs)+' }' if lhs else '∅'
                shown='、'.join(f'{_label(key)}={value}' for key,value in values[:4])
                conflicts.append(f'{lhs_text} → {a}：{shown}')
    return conflicts

def _cascade(rows,pending_key,plan):
    """Apply a correction to every row sharing the same frozen deciding set.

    A single-row edit can legitimately conflict with ``X → a`` when the group spans several
    commission rows. With an explicit request the caller accepts correcting the whole group;
    bounded passes keep the result deterministic, and the final check still rejects genuine
    violations.
    """
    rows=[{'key':row['key'],'values':dict(row['values'])} for row in rows]
    for _ in range(20):
        changed=False
        for fd in plan['dependencies']:
            lhs=list(fd.get('lhs') or []);rhs=list(fd.get('rhs') or [])
            if not rhs:continue
            groups={}
            for row in rows:groups.setdefault(tuple(row['values'].get(a) or '' for a in lhs),[]).append(row)
            for group in groups.values():
                if len(group)<2:continue
                for a in rhs:
                    values={row['values'].get(a) or '' for row in group}
                    if len(values)<2:continue
                    pending=[row for row in group if row['key']==pending_key]
                    source=pending[0] if pending else next((row for row in group if row['key']!=pending_key),group[0])
                    for row in group:
                        if row['values'].get(a)!=source['values'].get(a):
                            row['values'][a]=source['values'].get(a);changed=True
        if not changed:return rows
    return rows

def _reject_conflicts(rows,plan):
    conflicts=_conflicts(rows,plan['dependencies'])
    if conflicts:
        raise HTTPException(409,'改动违反报告冻结的函数依赖，已拒绝且未写入：'+'；'.join(conflicts[:3])+'。请更正确认冲突的委托行后重试。')

def _normalise_values(plan,values):
    attrs=plan['source_attributes'];unknown=set(values)-set(attrs);missing=set(attrs)-set(values)
    if unknown:raise HTTPException(422,'存在原表之外的字段：'+', '.join(sorted(unknown)))
    if missing:raise HTTPException(422,'缺少原表字段：'+', '.join(sorted(missing)))
    out={a:('' if values.get(a) is None else str(values.get(a))) for a in attrs}
    required=set().union(*[set(k) for k in plan.get('candidate_keys') or []]) or {attrs[0]}
    blank=sorted(required & {a for a in attrs if out[a]==''})
    if blank:raise HTTPException(422,'候选键字段不能为空：'+', '.join(blank))
    return out

def _journal(db,drills,row_key,action,before,after):
    for drill in drills:db.add(DrillChange(drill_id=drill.id,row_key=str(row_key),action=action,before=before,after=after))

def _target_upsert(conn,table,key,payload):
    """Race-safe upsert: update first, insert only when absent, retry the update on a lost
    insert race (a concurrent migration/write may insert the same source key)."""
    result=conn.execute(update(table).where(table.c._src_key==key).values(**payload))
    if result.rowcount:return
    try:
        with conn.begin_nested():conn.execute(table.insert().values(_src_key=key,**payload))
    except IntegrityError:
        conn.execute(update(table).where(table.c._src_key==key).values(**payload))

def _sync_target(conn,plan,key,values,action):
    """Keep target tables level with accepted source edits during migration.

    Migration still walks the full source, so an interrupted run catches up on its own; this
    only shortens the divergence window for reads and for the switch-time reconcile.
    """
    for rel in plan['relations']:
        table=_target_table(rel['table'],rel['attributes'])
        if action=='delete':
            conn.execute(delete(table).where(table.c._src_key==key))
        else:
            _target_upsert(conn,table,key,{a:values[a] for a in rel['attributes']})

def _reject_if_other_assignment(db,relation_id,drill_id=None):
    assignment=_assignment(db,relation_id,current=True)
    if assignment and assignment.drill_id!=drill_id:
        raise HTTPException(409,f'该委托表已由演练 #{assignment.drill_id} 切换生效，本次写入未生效；请回到生效演练操作')
    return assignment

def _write_source(db,plan,key,values,action,user,cascade=False):
    """Validate and write one source row; optionally cascade a correction across its FD group."""
    table=_source(plan)
    before={a:None for a in plan['source_attributes']}
    if key is not None:
        existing=db.execute(select(table).where(table.c._row_id==key)).first()
        if not existing:raise HTTPException(404,'委托行不存在')
        before={a:existing._mapping[a] for a in plan['source_attributes']}
    all_rows=_read_source(db.connection(),plan)
    active=_active_drills(db,plan['source_relation_id'])
    if action=='delete':
        if not any(r['key']==key for r in all_rows):raise HTTPException(404,'委托行不存在')
        _reject_conflicts([r for r in all_rows if r['key']!=key],plan)
        db.execute(delete(table).where(table.c._row_id==key))
        _sync_target(db.connection(),plan,str(key),None,'delete')
        _journal(db,active,key,action,before,None)
        db.add(Audit(actor=user.email,action=f'拆表演练委托行删除 {key}'))
        db.commit();return key
    pending_key=key if key is not None else 'new'
    candidate=[r for r in all_rows if r['key']!=key]+[{'key':pending_key,'values':values}]
    if cascade:
        candidate=_cascade(candidate,pending_key,plan)
        final_values=next(r['values'] for r in candidate if r['key']==pending_key)
        for row in candidate:
            if row['key']==pending_key or row['key'] not in {x['key'] for x in all_rows}:continue
            original=next(x['values'] for x in all_rows if x['key']==row['key'])
            if any(row['values'].get(a)!=original.get(a) for a in plan['source_attributes']):
                db.execute(update(table).where(table.c._row_id==row['key']).values(**row['values']))
                _sync_target(db.connection(),plan,str(row['key']),row['values'],'update')
                _journal(db,active,row['key'],'更正(同组)',original,row['values'])
        values=final_values
    _reject_conflicts(candidate,plan)
    if key is not None:
        db.execute(update(table).where(table.c._row_id==key).values(**values))
        _sync_target(db.connection(),plan,str(key),values,'update')
    else:
        result=db.execute(table.insert().values(**values));key=result.inserted_primary_key[0]
        _sync_target(db.connection(),plan,str(key),values,'create')
    _journal(db,active,key,action,before,values)
    db.add(Audit(actor=user.email,action=f'拆表演练委托行{action} '+str(key)))
    db.commit();return key

def _write_new(db,drill,key,values,action,user):
    plan=drill.plan
    current=_read_new(db.connection(),plan)
    if action=='delete':
        if not any(r['key']==key for r in current):raise HTTPException(404,'委托行不存在')
        _reject_conflicts([r for r in current if r['key']!=key],plan)
        for rel in plan['relations']:
            table=_target_table(rel['table'],rel['attributes'])
            db.execute(delete(table).where(table.c._src_key==key))
    else:
        _reject_conflicts([r for r in current if r['key']!=key]+[{'key':key or 'new','values':values}],plan)
        key=key or uuid.uuid4().hex
        for rel in plan['relations']:
            table=_target_table(rel['table'],rel['attributes'])
            _target_upsert(db.connection(),table,key,{a:values[a] for a in rel['attributes']})
    db.add(DrillChange(drill_id=drill.id,row_key=str(key),action=action+'(新表)',before=None,after=values))
    db.add(Audit(actor=user.email,action=f'新表委托行{action} '+str(key)))
    db.commit()
    return key

def _row_key(value):
    try:return int(value)
    except (TypeError,ValueError):raise HTTPException(422,'委托行编号必须是整数')

def mutate_row(db,ident,key,values,action,user,cascade=False):
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    _lock_relation(db,obj.relation_id)
    assignment=_reject_if_other_assignment(db,obj.relation_id,obj.id)
    if assignment:
        if action=='delete':
            if not key:raise HTTPException(422,'缺少委托行编号')
            _write_new(db,obj,key,None,action,user)
        else:_write_new(db,obj,key if key not in (None,'') else None,_normalise_values(obj.plan,values),action,user)
        return detail(db,ident)
    if obj.status not in ('pending','migrating','failed'):raise HTTPException(409,'演练当前状态不可写原表')
    if action=='delete':
        if not key:raise HTTPException(422,'缺少委托行编号')
        _write_source(db,obj.plan,_row_key(key),None,action,user)
    else:
        _write_source(db,obj.plan,_row_key(key) if key not in (None,'') else None,_normalise_values(obj.plan,values),action,user,cascade=bool(cascade))
    return detail(db,ident)

def _upsert_row(conn,plan,key,values):
    for rel in plan['relations']:
        table=_target_table(rel['table'],rel['attributes'])
        _target_upsert(conn,table,key,{a:values[a] for a in rel['attributes']})

def _reconcile(conn,plan,source_rows):
    keys=set()
    for row in source_rows:
        key=str(row['key']);keys.add(key);_upsert_row(conn,plan,key,row['values'])
    for rel in plan['relations']:
        table=_target_table(rel['table'],rel['attributes'])
        stale=[str(x) for x in conn.execute(select(table.c._src_key)).scalars() if str(x) not in keys]
        if stale:conn.execute(delete(table).where(table.c._src_key.in_(stale)))

def migrate(db,ident,user,max_rows=MAX_ROWS_PER_CALL):
    try:return _migrate(db,ident,user,max_rows)
    except HTTPException:
        db.rollback();raise
    except Exception:
        db.rollback();raise

def _migrate(db,ident,user,max_rows):
    """Idempotent batch backfill under the relation lock.

    Each call is one transaction: source rows are read ``FOR UPDATE``, every target row is
    upserted by ``_src_key`` and the cursor advances with the same commit. An interrupted call
    rolls back its uncommitted batch; already-committed batches stay and the next call resumes
    from the stored cursor without duplicating rows.
    """
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    _lock_relation(db,obj.relation_id)
    if _reject_if_other_assignment(db,obj.relation_id,obj.id):return _detail(obj)
    if obj.status=='switched':return _detail(obj)
    _create_tables(obj.plan)
    if obj.status in ('pending','failed'):obj.status=_assert_state('migrating')
    obj.error=None
    progress=dict(obj.progress or {});cursor=int(progress.get('cursor') or 0);copied=int(progress.get('copied') or 0)
    source_rows=_read_source(db.connection(),obj.plan,for_update=True)
    total=len(source_rows)
    pending=[r for r in source_rows if int(r['key'])>cursor]
    batch=pending[:max(1,min(int(max_rows or MAX_ROWS_PER_CALL),MAX_ROWS_PER_CALL))]
    for row in batch:
        _upsert_row(db.connection(),obj.plan,str(row['key']),row['values'])
        cursor=int(row['key']);copied+=1
    done=cursor>=max([int(r['key']) for r in source_rows] or [0])
    if done:_reconcile(db.connection(),obj.plan,source_rows)
    obj.progress={'cursor':cursor,'copied':copied,'total':total,'done':done}
    db.add(Audit(actor=user.email,action=f'拆表演练迁移 #{ident} 行 {copied}/{total}'))
    db.commit()
    return _detail(obj)

def switch(db,ident,user):
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    _lock_relation(db,obj.relation_id)
    assignment=_assignment(db,obj.relation_id,current=True)
    if assignment:
        if assignment.drill_id==obj.id:return _detail(obj)
        raise HTTPException(409,f'另一方案（演练 #{assignment.drill_id}）已先切换生效；本次方案未生效，原表仍可用')
    if not (obj.progress or {}).get('done'):
        raise HTTPException(409,'迁移尚未完成，不能切换；请继续迁移至全部委托行落库')
    plan=obj.plan
    conflicts=_conflicts(_read_source(db.connection(),plan),plan['dependencies'])
    if conflicts:
        obj.status=_assert_state('failed');obj.error='切换前校验发现违反报告依赖，已保留原表未切换：'+'；'.join(conflicts[:3]);db.commit()
        raise HTTPException(409,obj.error)
    obj.status=_assert_state('migrating');obj.error=None
    try:
        db.add(DrillAssignment(relation_id=obj.relation_id,drill_id=obj.id));db.flush()
    except IntegrityError:
        db.rollback();other=_assignment(db,obj.relation_id)
        raise HTTPException(409,f'并发切换冲突：演练 #{other.drill_id if other else "-"} 已占先，本次未生效，原表仍可用')
    source=_read_source(db.connection(),plan,for_update=True)
    _reconcile(db.connection(),plan,source)
    restored=_read_new(db.connection(),plan)
    source_checksum,source_tuples=_checksum(source,plan['source_attributes'])
    new_checksum,new_tuples=_checksum(restored,plan['source_attributes'])
    if Counter(source_tuples)!=Counter(new_tuples):
        db.rollback()
        obj=db.get(Drill,ident);obj.status=_assert_state('failed')
        obj.error='切换前还原校验失败：新表行与原表不一致，已保留原表'
        db.commit()
        raise HTTPException(409,obj.error)
    obj=db.get(Drill,ident);obj.status=_assert_state('switched');obj.error=None
    obj.source_checksum=source_checksum;obj.new_checksum=new_checksum;obj.row_count=len(source)
    obj.switched_at=datetime.now(timezone.utc)
    obj.progress={**(obj.progress or {}),'done':True,'total':len(source),'copied':len(source)}
    db.add(Audit(actor=user.email,action=f'拆表演练切换生效 #{ident}'))
    db.commit()
    return _detail(obj)

def compare(db,ident):
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    plan=obj.plan
    with engine.connect() as conn:
        source=_read_source_safe(conn,plan);new=_read_new_safe(conn,plan)
    source_checksum,source_tuples=_checksum(source,plan['source_attributes'])
    new_checksum,new_tuples=_checksum(new,plan['source_attributes'])
    source_counter,new_counter=Counter(source_tuples),Counter(new_tuples)
    return {'source_count':len(source),'new_count':len(new),'equal':source_counter==new_counter,
            'source_checksum':source_checksum,'new_checksum':new_checksum,
            'missing_from_new':[dict(zip(plan['source_attributes'],x)) for x in list((source_counter-new_counter).elements())[:10]],
            'extra_in_new':[dict(zip(plan['source_attributes'],x)) for x in list((new_counter-source_counter).elements())[:10]],
            'source_rows':[dict(zip(plan['source_attributes'],x)) for x in source_tuples],
            'new_rows':[dict(zip(plan['source_attributes'],x)) for x in new_tuples],
            'attributes':plan['source_attributes'],'new_tables':[x['table'] for x in plan['relations']],
            'source_table':source_table_name(obj.relation_id)}

def tables(db,ident):
    """Physical table inspection for the rehearsal page: columns and (bounded) sample rows."""
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    plan=obj.plan;names=[source_table_name(obj.relation_id)]+[r['table'] for r in plan['relations']]
    out=[]
    with engine.connect() as conn:
        insp=inspect(conn)
        for name in names:
            exists=insp.has_table(name)
            cols=[c['name'] for c in insp.get_columns(name)] if exists else []
            rows_=[]
            if exists:
                table=Table(name,MetaData(),autoload_with=conn)
                for row in conn.execute(select(table).limit(5)).mappings():
                    rows_.append({k:(None if v is None else str(v)) for k,v in row.items()})
            out.append({'table':name,'columns':cols,'sample':rows_})
    return {'source_table':names[0],'new_tables':names[1:],'tables':out}

def changes(db,ident):
    obj=db.get(Drill,ident)
    if not obj:raise HTTPException(404,'演练不存在')
    return [record(x) for x in db.scalars(select(DrillChange).where(DrillChange.drill_id==ident).order_by(DrillChange.id.desc()).limit(100))]
