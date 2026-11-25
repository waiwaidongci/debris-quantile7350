"""岸巡 / 完工双台账、幂等批次入账与按指定时点合表。

两条台账分别接收记录。批次以规范化内容的 SHA-256 去重：同一批次重试不会重复
入账；同一工单同一版本出现不同内容时整批退回。合表任务在受理时冻结两边台账的
快照，之后无论补正何时到达，续跑都只使用该快照，因此中断重试不会读到新数据。
每个任务最多生成一份成表，成表以数据版本的形式持久化并供分析引用。补正后可以
按新的快照另建任务生成新版成表，旧成表照常可查、可用于分析。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.constants.enums import State
from src.models.database import add_event, engine
from src.models.tables import (datasets, ledger_batches, ledger_records, merge_tasks,
                               merged_tables)
from src.services.data import PROFILE, save_revision
from src.services.domain import DONE, PENDING, TIME_FORMAT, sample_rows

PATROL = 'patrol'
COMPLETION = 'completion'
LEDGERS = {PATROL: '岸巡台账', COMPLETION: '完工台账'}
POST = 'post'
REVOKE = 'revoke'
ACTIONS = {POST: '入账', REVOKE: '撤销'}
ACTION_ALIASES = {POST: POST, REVOKE: REVOKE, '入账': POST, '撤销': REVOKE}
CSV_COLUMNS = {
    PATROL: ['order_id', 'version', 'action', 'posted_at', 'effective_at',
             'reach', 'planned_start', 'snapshot_at', 'debris_m3'],
    COMPLETION: ['order_id', 'version', 'action', 'posted_at', 'effective_at', 'duration_min'],
}
FIELD_ALIASES = {
    'order_id': ('order_id', '工单号'),
    'version': ('version', '版本'),
    'action': ('action', '动作', 'operation'),
    'posted_at': ('posted_at', 'posted', 'recorded_at', '入账时间', '完工入账时间'),
    'effective_at': ('effective_at', 'effective', '生效时间'),
    'reach': ('reach', '河段', '作业河段'),
    'planned_start': ('planned_start', '计划开工', '计划开工（北京时间）'),
    'snapshot_at': ('snapshot_at', '巡查快照', '冻结巡查时间'),
    'debris_m3': ('debris_m3', '预估漂浮物量', '预估漂浮物量（m³）'),
    'duration_min': ('duration_min', '实耗', '作业实耗', '作业实耗（分钟）'),
}
RESUME_WINDOW = 120


def _time(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label}请填写北京时间 YYYY-MM-DD HH:MM。')
    try:
        parsed = datetime.strptime(value.strip(), TIME_FORMAT)
    except (ValueError, TypeError) as error:
        raise ValueError(f'{label}请填写北京时间 YYYY-MM-DD HH:MM。') from error
    if parsed.year < 1900:
        raise ValueError(f'{label}请填写 1900 年起的北京时间 YYYY-MM-DD HH:MM。')
    return parsed


def to_epoch(value):
    return value.replace(tzinfo=timezone.utc).timestamp()


def _number(value, low, high, label):
    if isinstance(value, bool):
        raise ValueError(f'{label}请填写 {low} 至 {high} 之间的有限数值并核对单位。')
    if isinstance(value, str):
        value = value.strip()
        try:
            value = float(value)
        except ValueError as error:
            raise ValueError(f'{label}请填写 {low} 至 {high} 之间的有限数值并核对单位。') from error
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f'{label}请填写 {low} 至 {high} 之间的有限数值并核对单位。')
    return float(value)


def _version(value):
    if isinstance(value, str) and re.fullmatch(r'\d{1,6}', value.strip()):
        value = int(value.strip())
    if type(value) is not int or not 1 <= value <= 999999:
        raise ValueError('版本请填写 1 至 999999 的整数。')
    return value


def _action(value):
    if isinstance(value, str):
        action = ACTION_ALIASES.get(value.strip())
        if action:
            return action
    raise ValueError('动作请填写 post（入账）或 revoke（撤销）。')


def _payload(kind, raw, line, code=''):
    if kind == PATROL:
        reach = _pick(raw, 'reach', '河段', '作业河段')
        if not isinstance(reach, str) or not reach.strip():
            raise ValueError(f'第 {line} 行请补填岸巡台账的作业河段。')
        if len(reach.strip()) > 80:
            raise ValueError(f'第 {line} 行作业河段过长。')
        start = _time(_pick(raw, 'planned_start', '计划开工'), f'第 {line} 行计划开工')
        snapshot = _time(_pick(raw, 'snapshot_at', '巡查快照', '冻结巡查时间'),
                         f'第 {line} 行冻结巡查时间')
        if snapshot > start:
            raise ValueError(f'第 {line} 行巡查快照晚于计划开工，请换成开工前已留存的快照。')
        load = _number(_pick(raw, 'debris_m3', '预估漂浮物量'), 0, 1000,
                       f'第 {line} 行预估漂浮物量（m³）')
        return {
            'reach': reach.strip(), 'planned_start': start.strftime(TIME_FORMAT),
            'snapshot_at': snapshot.strftime(TIME_FORMAT), 'debris_m3': round(load, 6),
        }
    duration = _number(_pick(raw, 'duration_min', '实耗', '作业实耗'), 1, 1440,
                       f'第 {line} 行完工实耗（分钟）')
    return {'duration_min': round(duration, 6)}


def _pick(raw, *names):
    for name in names:
        if name in raw and raw[name] not in (None, ''):
            return raw[name]
    return None


def normalize_record(kind, raw, line):
    if kind not in LEDGERS:
        raise HTTPException(404, '台账类型不存在。')
    if not isinstance(raw, dict):
        raise ValueError(f'第 {line} 行记录格式有误。')
    code = _pick(raw, 'order_id', '工单号')
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f'第 {line} 行请补填工单号。')
    code = code.strip()
    if len(code) > 80:
        raise ValueError(f'第 {line} 行工单号过长。')
    version = _version(_pick(raw, 'version', '版本'))
    action = _action(_pick(raw, 'action', '动作', 'operation') or POST)
    posted_value = _pick(raw, 'posted_at', 'posted', 'recorded_at', '入账时间', '工序入账时间')
    posted = _time(posted_value, f'第 {line} 行入账时间')
    effective_value = _pick(raw, 'effective_at', 'effective', '生效时间')
    effective = _time(effective_value, f'第 {line} 行生效时间') if effective_value is not None else posted
    payload = {} if action == REVOKE else _payload(kind, raw, line, code)
    content = {
        'action': action, 'posted_at': posted.strftime(TIME_FORMAT),
        'effective_at': effective.strftime(TIME_FORMAT), 'payload': payload,
    }
    return {
        'order_id': code, 'version': version, 'action': action,
        'posted_epoch': to_epoch(posted), 'effective_epoch': to_epoch(effective),
        'posted_text': content['posted_at'], 'effective_text': content['effective_at'],
        'payload': payload, 'checksum': _digest(content),
    }


def _digest(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def parse_ledger_csv(kind, text):
    if kind not in LEDGERS:
        raise HTTPException(404, '台账类型不存在。')
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿'), newline=''), strict=True)
    expected = CSV_COLUMNS[kind]
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('CSV 表头的引号格式有误。') from error
    if not fieldnames:
        raise ValueError('CSV 缺少表头。')
    mapping = {}
    for name in fieldnames:
        canonical = next((key for key, aliases in FIELD_ALIASES.items() if name in aliases), None)
        if canonical is None:
            raise ValueError(f'CSV 出现未知列 {name}。')
        if canonical not in expected:
            raise ValueError(f'{LEDGERS[kind]}不接受 {name} 列。')
        if canonical in mapping.values():
            raise ValueError(f'CSV 列 {name} 重复。')
        mapping[name] = canonical
    required = {'order_id', 'version', 'action', 'posted_at'}
    missing = required - set(mapping.values())
    if missing:
        labels = '、'.join(next(aliases[0] for key, aliases in FIELD_ALIASES.items() if key == item)
                           for item in sorted(missing))
        raise ValueError('CSV 缺少列：' + labels)
    rows = []
    try:
        for row in reader:
            if None in row:
                raise ValueError(f'第 {reader.line_num} 行列数多于表头。')
            rows.append({mapping[name]: value for name, value in row.items() if name in mapping})
            if len(rows) > 5000:
                raise ValueError('单份数据最多 5000 行。')
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('CSV 需要至少一条数据。')
    return rows


def post_batch(kind, name, text, records, actor):
    """幂等接收一批台账记录。返回本批新入账与重复的条数。"""
    if kind not in LEDGERS:
        raise HTTPException(404, '台账类型不存在。')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('批次名称不能为空。')
    if (text is None) == (records is None):
        raise ValueError('请提供 CSV 文本或记录列表其一。')
    raw = parse_ledger_csv(kind, text) if text is not None else records
    if not isinstance(raw, list) or not raw:
        raise ValueError('批次需要至少一条记录。')
    first_line = 2 if text is not None else 1
    normalized = [normalize_record(kind, item, line) for line, item in enumerate(raw, first_line)]
    unique = {}
    for record in normalized:
        key = (record['order_id'], record['version'])
        kept = unique.get(key)
        if kept is not None and kept['checksum'] != record['checksum']:
            raise ValueError(f'工单 {key[0]} 版本 {key[1]} 在同一批次内内容冲突，已退回整批。')
        unique[key] = record
    records = sorted(unique.values(), key=lambda item: (item['order_id'], item['version']))
    checksum = _digest({
        'ledger': kind,
        'records': [{'order_id': item['order_id'], 'version': item['version'],
                     'checksum': item['checksum']} for item in records],
    })
    now = time.time()
    for attempt in (1, 2):
        try:
            return _insert_batch(kind, name, records, checksum, actor, now,
                                 report_existing=attempt == 2)
        except IntegrityError:
            # 并发提交同一或重叠批次：第二轮重新读取已入账版本并只补新版本。
            continue
    with engine.connect() as connection:
        existing = connection.execute(select(ledger_batches).where(
            ledger_batches.c.ledger == kind, ledger_batches.c.checksum == checksum)).mappings().one_or_none()
    if existing is not None:
        return _batch_response(existing, created=False, inserted=0, ledger=kind)
    raise HTTPException(409, '同单同版内容冲突，已退回整批，请核对后重新提交。')


def _insert_batch(kind, name, records, checksum, actor, now, report_existing):
    with engine.begin() as connection:
        if report_existing:
            existing = connection.execute(select(ledger_batches).where(
                ledger_batches.c.ledger == kind,
                ledger_batches.c.checksum == checksum)).mappings().one_or_none()
            if existing is not None:
                return _batch_response(existing, created=False, inserted=0, ledger=kind)
        codes = sorted({item['order_id'] for item in records})
        known = {
            (row['order_id'], row['version']): row['checksum']
            for row in connection.execute(select(
                ledger_records.c.order_id, ledger_records.c.version, ledger_records.c.checksum).where(
                ledger_records.c.ledger == kind, ledger_records.c.order_id.in_(codes))).mappings()
        }
        conflicts, fresh = [], []
        for item in records:
            seen = known.get((item['order_id'], item['version']))
            if seen is None:
                fresh.append(item)
            elif seen != item['checksum']:
                conflicts.append(f'{item["order_id"]} 版本 {item["version"]}')
        if conflicts:
            raise HTTPException(409, '以下工单同单同版内容冲突，已退回整批：'
                                + '、'.join(conflicts) + '。')
        batch_id = connection.execute(insert(ledger_batches).values(
            ledger=kind, checksum=checksum, name=name.strip(), owner=actor, accepted_at=now,
            record_count=len(records), inserted_count=len(fresh))).inserted_primary_key[0]
        if fresh:
            connection.execute(insert(ledger_records), [{
                'ledger': kind, 'order_id': item['order_id'], 'version': item['version'],
                'action': item['action'], 'posted_at': item['posted_epoch'],
                'effective_at': item['effective_epoch'], 'posted_text': item['posted_text'],
                'effective_text': item['effective_text'], 'payload': item['payload'],
                'checksum': item['checksum'], 'batch_id': batch_id, 'received_at': now,
            } for item in fresh])
        add_event(connection, actor, 'ledger', batch_id, now)
        return {
            'batch_id': batch_id, 'ledger': kind, 'label': LEDGERS[kind], 'created': True,
            'received': len(records), 'inserted': len(fresh),
            'duplicates': len(records) - len(fresh), 'conflicts': [],
            'record_count': len(records), 'inserted_count': len(fresh),
        }


def _batch_response(row, created, inserted, ledger):
    return {
        'batch_id': row['id'], 'ledger': ledger, 'label': LEDGERS[ledger], 'created': created,
        'received': row['record_count'], 'inserted': inserted,
        'duplicates': row['record_count'] - inserted,
        'record_count': row['record_count'], 'inserted_count': row['inserted_count'],
    }


def _record_view(row):
    view = {
        'order_id': row['order_id'], 'version': row['version'], 'action': row['action'],
        'action_label': ACTIONS.get(row['action'], row['action']),
        'posted_at': row['posted_text'], 'effective_at': row['effective_text'],
        'batch_id': row['batch_id'],
    }
    view.update(row['payload'] or {})
    return view


def ledger_summary(kind):
    if kind not in LEDGERS:
        raise HTTPException(404, '台账类型不存在。')
    with engine.connect() as connection:
        batches = [dict(row) for row in connection.execute(
            select(ledger_batches).where(ledger_batches.c.ledger == kind)
            .order_by(ledger_batches.c.id.desc()).limit(20)).mappings()]
        records = [_record_view(row) for row in connection.execute(
            select(ledger_records).where(ledger_records.c.ledger == kind)
            .order_by(ledger_records.c.id.desc()).limit(80)).mappings()]
    return {'label': LEDGERS[kind], 'kind': kind, 'batches': batches, 'records': records}


def all_ledgers():
    return {kind: ledger_summary(kind) for kind in LEDGERS}


def _snapshot(connection):
    rows = connection.execute(select(
        ledger_records.c.ledger, ledger_records.c.order_id, ledger_records.c.version,
        ledger_records.c.action, ledger_records.c.posted_at, ledger_records.c.effective_at,
        ledger_records.c.posted_text, ledger_records.c.effective_text, ledger_records.c.payload,
    ).order_by(ledger_records.c.ledger, ledger_records.c.order_id, ledger_records.c.version)).mappings()
    return [{
        'ledger': row['ledger'], 'order_id': row['order_id'], 'version': row['version'],
        'action': row['action'], 'posted': row['posted_at'], 'effective': row['effective_at'],
        'posted_text': row['posted_text'], 'effective_text': row['effective_text'],
        'payload': row['payload'],
    } for row in rows]


def request_merge(name, as_of_text, actor):
    as_of = _time(as_of_text, '合表时点')
    stamp = as_of.strftime(TIME_FORMAT)
    now = time.time()
    label = (name.strip() if isinstance(name, str) and name.strip()
             else f"{PROFILE['filename'].removesuffix('.csv')} · 合表 {stamp}")
    for attempt in (1, 2):
        with engine.begin() as connection:
            snapshot = _snapshot(connection)
            checksum = _digest(snapshot)
            request_key = _digest({'as_of': stamp, 'snapshot': checksum})
            found = connection.execute(select(merge_tasks.c.id, merge_tasks.c.state).where(
                merge_tasks.c.request_key == request_key)).mappings().one_or_none()
            if found is not None:
                return {'id': found['id'], 'state': found['state'], 'created': False, 'as_of': stamp}
            try:
                key = connection.execute(insert(merge_tasks).values(
                    as_of=to_epoch(as_of), as_of_text=stamp, name=label, owner=actor,
                    state=State.queued.value, created=now, request_key=request_key,
                    source_checksum=checksum, source_count=len(snapshot),
                    snapshot=snapshot)).inserted_primary_key[0]
            except IntegrityError:
                # 并发受理相同快照与时点：下一轮读取已建立的任务。
                continue
            add_event(connection, actor, 'merge', key, now)
            return {'id': key, 'state': State.queued.value, 'created': True, 'as_of': stamp}
    with engine.connect() as connection:
        found = connection.execute(select(merge_tasks.c.id, merge_tasks.c.state).where(
            merge_tasks.c.request_key == request_key)).mappings().one_or_none()
    if found is None:
        raise HTTPException(409, '合表任务并发受理冲突，请重试。')
    return {'id': found['id'], 'state': found['state'], 'created': False, 'as_of': stamp}


def claim_merge():
    now = time.time()
    claimable = or_(merge_tasks.c.state == State.queued.value,
                    and_(merge_tasks.c.state == State.running.value,
                         merge_tasks.c.started < now - RESUME_WINDOW))
    with engine.begin() as connection:
        task = connection.execute(select(merge_tasks).where(claimable)
                                  .order_by(merge_tasks.c.id).limit(1)).mappings().one_or_none()
        if task is None:
            return None
        claimed = connection.execute(update(merge_tasks).where(merge_tasks.c.id == task['id'], claimable)
                                     .values(state=State.running.value, started=now))
        if claimed.rowcount != 1:
            return None
        job = dict(task)
        job['started'] = now
        return job


def build_rows(snapshot, as_of):
    """按受理时冻结的快照合表：只取 as_of 时已入账且已生效的最高版本。"""
    visible = {}
    for record in snapshot:
        if record['posted'] > as_of or record['effective'] > as_of:
            continue
        key = (record['ledger'], record['order_id'])
        kept = visible.get(key)
        if kept is None or record['version'] > kept['version']:
            visible[key] = record
    patrol = {order: record for (ledger, order), record in visible.items() if ledger == PATROL}
    completion = {order: record for (ledger, order), record in visible.items() if ledger == COMPLETION}
    orphans = sorted(order for order, record in completion.items()
                     if order not in patrol and record['action'] != REVOKE)
    if orphans:
        preview = '、'.join(orphans[:5]) + (' 等' if len(orphans) > 5 else '')
        raise ValueError(f'完工台账中的 {preview} 在该时点没有岸巡记录，请补登岸巡台账后重新合表。')
    rows = []
    for order, record in patrol.items():
        if record['action'] == REVOKE:
            continue
        payload = record['payload']
        done = completion.get(order)
        if done is None or done['action'] == REVOKE:
            status, duration, recorded = PENDING, 0.0, PENDING
        else:
            status, duration, recorded = DONE, float(done['payload']['duration_min']), done['posted_text']
        rows.append({
            'order_id': order, 'reach': payload['reach'],
            'planned_start': payload['planned_start'], 'snapshot_at': payload['snapshot_at'],
            'debris_m3': payload['debris_m3'], 'status': status,
            'duration_min': duration, 'recorded_at': recorded,
        })
    rows.sort(key=lambda row: (row['planned_start'], row['order_id']))
    if not rows:
        raise ValueError('合表时点前没有可用的岸巡工单，请核对入账与生效时间。')
    return rows


def finish_merge(key, claim, dataset_id, row_count, error):
    now = time.time()
    state = State.failed.value if error else State.completed.value
    with engine.begin() as connection:
        saved = connection.execute(update(merge_tasks).where(
            merge_tasks.c.id == key, merge_tasks.c.started == claim,
            merge_tasks.c.state == State.running.value).values(
            state=state, dataset_id=dataset_id, row_count=row_count, error=error, finished=now))
        if saved.rowcount:
            add_event(connection, 'worker', state, key, now)


def process_merge(task):
    """处理一个已领取的合表任务。重复处理只会复用已生成的那份成表。"""
    key, claim = task['id'], task['started']
    try:
        with engine.connect() as connection:
            existing = connection.execute(select(
                merged_tables.c.dataset_id, merged_tables.c.row_count).where(
                merged_tables.c.task_id == key)).mappings().one_or_none()
        if existing is not None:
            finish_merge(key, claim, existing['dataset_id'], existing['row_count'], None)
            return
        rows = build_rows(task['snapshot'], task['as_of'])
        dataset_id, _ = save_revision(task['name'], rows, 'merge')
        try:
            with engine.begin() as connection:
                connection.execute(insert(merged_tables).values(
                    task_id=key, dataset_id=dataset_id, name=task['name'], as_of=task['as_of'],
                    as_of_text=task['as_of_text'], row_count=len(rows),
                    content_checksum=_digest(rows), created=time.time()))
        except IntegrityError:
            with engine.connect() as connection:
                dataset_id = connection.execute(select(merged_tables.c.dataset_id).where(
                    merged_tables.c.task_id == key)).scalar()
            if dataset_id is None:
                raise
        finish_merge(key, claim, dataset_id, len(rows), None)
    except ValueError as error:
        finish_merge(key, claim, None, None, str(error))
    except Exception:
        logging.exception('Merge %s failed', key)
        finish_merge(key, claim, None, None, '合表失败，请核对台账后重新提交。')


def run_merge(key):
    """立即处理合表任务（用于 seed 或明确的续跑请求）。"""
    now = time.time()
    resumable = or_(
        merge_tasks.c.state.in_([State.queued.value, State.failed.value]),
        and_(merge_tasks.c.state == State.running.value,
             merge_tasks.c.started < now - RESUME_WINDOW))
    with engine.begin() as connection:
        claimed = connection.execute(update(merge_tasks).where(
            merge_tasks.c.id == key, resumable).values(state=State.running.value, started=now))
        if claimed.rowcount != 1:
            task = connection.execute(select(merge_tasks.c.state, merge_tasks.c.dataset_id)
                                      .where(merge_tasks.c.id == key)).mappings().one_or_none()
            if task is None:
                raise HTTPException(404, '合表任务不存在。')
            return dict(task)
        task = dict(connection.execute(select(merge_tasks).where(
            merge_tasks.c.id == key)).mappings().one())
    task['started'] = now
    process_merge(task)
    with engine.connect() as connection:
        return dict(connection.execute(select(merge_tasks.c.state, merge_tasks.c.dataset_id)
                                       .where(merge_tasks.c.id == key)).mappings().one())


def list_merge_tables():
    statement = select(
        merge_tasks.c.id, merge_tasks.c.name, merge_tasks.c.as_of_text, merge_tasks.c.state,
        merge_tasks.c.created, merge_tasks.c.finished, merge_tasks.c.dataset_id,
        merge_tasks.c.row_count, merge_tasks.c.error, merge_tasks.c.source_count,
        merge_tasks.c.owner, merged_tables.c.id.label('table_id'),
        datasets.c.checksum.label('dataset_checksum'), datasets.c.created.label('dataset_created'),
    ).select_from(
        merge_tasks.outerjoin(merged_tables, merged_tables.c.task_id == merge_tasks.c.id)
        .outerjoin(datasets, datasets.c.id == merge_tasks.c.dataset_id)
    ).order_by(merge_tasks.c.id.desc()).limit(100)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement).mappings()]


def merge_task(key):
    with engine.connect() as connection:
        task = connection.execute(select(merge_tasks).where(merge_tasks.c.id == key)).mappings().one_or_none()
    if task is None:
        raise HTTPException(404, '合表任务不存在。')
    detail = dict(task)
    detail.pop('snapshot', None)
    detail.pop('request_key', None)
    return detail


def merged_dataset(connection, dataset_id):
    """成表数据集可能被多个内容相同的合表任务引用，取其一即可。"""
    return connection.execute(select(merged_tables.c.id).where(
        merged_tables.c.dataset_id == dataset_id).limit(1)).first()


def _patrol_row(row):
    return {
        'order_id': row['order_id'], 'version': 1, 'action': POST,
        'posted_at': row['snapshot_at'], 'effective_at': row['snapshot_at'],
        'reach': row['reach'], 'planned_start': row['planned_start'],
        'snapshot_at': row['snapshot_at'], 'debris_m3': row['debris_m3'],
    }


def _completion_row(row):
    return {
        'order_id': row['order_id'], 'version': 1, 'action': POST,
        'posted_at': row['recorded_at'], 'effective_at': row['recorded_at'],
        'duration_min': row['duration_min'],
    }


def sample_ledger_rows(kind):
    """演示用台账记录：岸巡含全部工单，完工只含已完工工单。"""
    rows = sample_rows()
    selected = rows if kind == PATROL else [row for row in rows if row['status'] == DONE]
    build = _patrol_row if kind == PATROL else _completion_row
    return [build(row) for row in selected], CSV_COLUMNS[kind]
