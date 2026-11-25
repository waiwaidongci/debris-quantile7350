from fastapi import HTTPException
from sqlalchemy import select

from src.models.database import engine, fetch_one
from src.models.tables import audit, datasets, deliveries, merge_tasks, merged_tables, runs, schedules


def list_rows(table, fields=None, limit=100):
    statement = select(*fields) if fields else select(table)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement.order_by(table.c.id.desc()).limit(limit)).mappings()]


def revisions():
    columns = [field for field in datasets.c if field.name != 'records']
    merged_task = select(merged_tables.c.task_id).where(
        merged_tables.c.dataset_id == datasets.c.id
    ).order_by(merged_tables.c.id).limit(1).correlate(datasets).scalar_subquery()
    statement = select(
        *columns, merged_task.label('merge_task_id'),
        merge_tasks.c.as_of_text, merge_tasks.c.row_count.label('merged_rows'),
    ).select_from(
        datasets.outerjoin(merged_tables, merged_tables.c.task_id == merged_task)
        .outerjoin(merge_tasks, merge_tasks.c.id == merged_tables.c.task_id)
    ).order_by(datasets.c.id.desc()).limit(100)
    with engine.connect() as connection:
        rows = []
        seen = set()
        for row in connection.execute(statement).mappings():
            item = dict(row)
            if item['id'] in seen:
                continue
            seen.add(item['id'])
            item['merged'] = item.pop('merge_task_id') is not None
            item['count'] = item.pop('merged_rows') if item['merged'] else None
            rows.append(item)
        return rows


def require_row(table, key, label):
    with engine.connect() as connection:
        row = fetch_one(connection, table, key)
    if row is None:
        raise HTTPException(404, label + '不存在。')
    return dict(row)


def jobs():
    return list_rows(runs, [field for field in runs.c if field.name != 'result'])


def delivery_status():
    return {'schedules': list_rows(schedules), 'deliveries': list_rows(deliveries)}


def recent_activity():
    return list_rows(audit, limit=20)
