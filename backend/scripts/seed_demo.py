import json

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import Role
from src.models.database import engine
from src.models.tables import datasets, merge_tasks, users
from src.services.data import PROFILE
from src.services.domain import DONE, sample_rows
from src.services.ledger import (COMPLETION, PATROL, _completion_row, _patrol_row, post_batch,
                                 request_merge, run_merge)
from src.utils.security import protect

DEMO_MERGE_AS_OF = '2026-11-23 18:00'


def seed():
    for role in Role:
        try:
            with engine.begin() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    connection.execute(insert(users).values(username=role.value, role=role.value,
                                                            password=protect(f'{role.value}-demo-24')))
        except IntegrityError:
            with engine.connect() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    raise
    rows = sample_rows()
    patrol = post_batch(PATROL, '演示 · 岸巡台账', None,
                        [_patrol_row(row) for row in rows], 'seed')
    completion = post_batch(COMPLETION, '演示 · 完工台账', None,
                            [_completion_row(row) for row in rows if row['status'] == DONE], 'seed')
    task = request_merge(f"{PROFILE['filename'].removesuffix('.csv')} · 演示成表",
                         DEMO_MERGE_AS_OF, 'seed')
    merged = run_merge(task['id'])
    with engine.connect() as connection:
        counts = {table.name: connection.scalar(select(func.count()).select_from(table))
                  for table in (users, datasets, merge_tasks)}
    return {
        'revision': merged.get('dataset_id'), 'merge': task['id'],
        'patrol_inserted': patrol['inserted'], 'completion_inserted': completion['inserted'],
        **counts, 'rows': len(rows),
    }


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
