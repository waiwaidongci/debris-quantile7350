from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
runs = Table('analysis_runs', metadata, Column('id', Integer, primary_key=True),
             Column('dataset_id', Integer, nullable=False, index=True), Column('options', JSON, nullable=False),
             Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
             Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
             Column('result', JSON), Column('error', Text), Column('decision', String(16)),
             Column('reviewer', String(40)), Column('review_note', Text), Column('reviewed', Double))
schedules = Table('report_schedules', metadata, Column('id', Integer, primary_key=True),
                  Column('run_id', Integer, nullable=False), Column('recipient', String(100), nullable=False),
                  Column('cadence', String(16), nullable=False), Column('next_at', Double, nullable=False, index=True),
                  Column('active', Integer, nullable=False), Column('owner', String(40), nullable=False))
deliveries = Table('report_deliveries', metadata, Column('id', Integer, primary_key=True),
                   Column('schedule_id', Integer, nullable=False), Column('run_id', Integer, nullable=False),
                   Column('recipient', String(100), nullable=False), Column('scheduled', Double, nullable=False),
                   Column('delivered', Double, nullable=False), UniqueConstraint('schedule_id', 'scheduled'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))
ledger_batches = Table(
    'ledger_batches', metadata,
    Column('id', Integer, primary_key=True),
    Column('ledger', String(16), nullable=False, index=True),
    Column('checksum', String(64), nullable=False),
    Column('name', String(180), nullable=False),
    Column('owner', String(40), nullable=False),
    Column('accepted_at', Double, nullable=False),
    Column('record_count', Integer, nullable=False),
    Column('inserted_count', Integer, nullable=False),
    UniqueConstraint('ledger', 'checksum', name='uq_ledger_batch_checksum'))
ledger_records = Table(
    'ledger_records', metadata,
    Column('id', Integer, primary_key=True),
    Column('ledger', String(16), nullable=False, index=True),
    Column('order_id', String(80), nullable=False, index=True),
    Column('version', Integer, nullable=False),
    Column('action', String(16), nullable=False),
    Column('posted_at', Double, nullable=False),
    Column('effective_at', Double, nullable=False),
    Column('posted_text', String(20), nullable=False),
    Column('effective_text', String(20), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('checksum', String(64), nullable=False),
    Column('batch_id', Integer, nullable=False),
    Column('received_at', Double, nullable=False),
    UniqueConstraint('ledger', 'order_id', 'version', name='uq_ledger_record_version'))
merge_tasks = Table(
    'merge_tasks', metadata,
    Column('id', Integer, primary_key=True),
    Column('as_of', Double, nullable=False),
    Column('as_of_text', String(20), nullable=False),
    Column('name', String(180), nullable=False),
    Column('owner', String(40), nullable=False),
    Column('state', String(16), nullable=False, index=True),
    Column('created', Double, nullable=False),
    Column('started', Double),
    Column('finished', Double),
    Column('request_key', String(64), nullable=False, unique=True),
    Column('source_checksum', String(64), nullable=False),
    Column('source_count', Integer, nullable=False),
    Column('snapshot', JSON, nullable=False),
    Column('dataset_id', Integer),
    Column('row_count', Integer),
    Column('error', Text))
merged_tables = Table(
    'merged_tables', metadata,
    Column('id', Integer, primary_key=True),
    Column('task_id', Integer, nullable=False, unique=True),
    Column('dataset_id', Integer, nullable=False, index=True),
    Column('name', String(180), nullable=False),
    Column('as_of', Double, nullable=False),
    Column('as_of_text', String(20), nullable=False),
    Column('row_count', Integer, nullable=False),
    Column('content_checksum', String(64), nullable=False),
    Column('created', Double, nullable=False))
