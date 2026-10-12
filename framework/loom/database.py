import contextlib
import json
import sqlite3
from pathlib import Path


SCHEMA = '''
CREATE TABLE IF NOT EXISTS policy(singleton INTEGER PRIMARY KEY CHECK(singleton=1),body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,owner TEXT NOT NULL,scope TEXT NOT NULL,kind TEXT NOT NULL,created TEXT NOT NULL,path TEXT NOT NULL,audience TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,digest TEXT,pending TEXT,UNIQUE(scope,path));
CREATE TABLE IF NOT EXISTS commits(id TEXT PRIMARY KEY,document TEXT NOT NULL,base INTEGER NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,stage TEXT NOT NULL,state TEXT NOT NULL,task TEXT NOT NULL,actor TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY,document TEXT NOT NULL,base INTEGER NOT NULL,current INTEGER NOT NULL,intent_path TEXT NOT NULL,task TEXT NOT NULL,actor TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(document TEXT PRIMARY KEY,holder TEXT NOT NULL,fence INTEGER NOT NULL,active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,scope TEXT NOT NULL,actor TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,event TEXT,session TEXT,epoch INTEGER NOT NULL,result_commit TEXT,cancelled INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,task TEXT NOT NULL,scope TEXT NOT NULL,epoch INTEGER NOT NULL,payload_document TEXT NOT NULL,payload_revision INTEGER NOT NULL,payload_digest TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,platform TEXT NOT NULL,tenant TEXT NOT NULL,chat TEXT NOT NULL,message TEXT NOT NULL,resource TEXT,UNIQUE(platform,tenant,chat,message));
CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,message TEXT NOT NULL,app TEXT NOT NULL,event TEXT NOT NULL,triggered INTEGER NOT NULL,task TEXT,UNIQUE(app,event));
CREATE TABLE IF NOT EXISTS processing(message TEXT NOT NULL,app TEXT NOT NULL,scope TEXT NOT NULL,task TEXT NOT NULL,PRIMARY KEY(message,app,scope));
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,version INTEGER NOT NULL,roster_version INTEGER NOT NULL,voters TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,approval_rule TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposal_guards(proposal TEXT PRIMARY KEY,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unknown_contacts(key TEXT PRIMARY KEY,app TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,count INTEGER NOT NULL,last_notice REAL,chat_type TEXT NOT NULL,tail TEXT NOT NULL DEFAULT '',notices INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS unknown_notices(id TEXT PRIMARY KEY,key TEXT NOT NULL,n INTEGER NOT NULL,person TEXT NOT NULL,scope TEXT NOT NULL,state TEXT NOT NULL,outbox TEXT);
CREATE TABLE IF NOT EXISTS unknown_decisions(key TEXT NOT NULL,decider TEXT NOT NULL,decision TEXT NOT NULL,at TEXT NOT NULL,message TEXT,PRIMARY KEY(key,decider));
CREATE TABLE IF NOT EXISTS host_commits(id TEXT PRIMARY KEY,task TEXT NOT NULL,document TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS message_actions(outbox TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,version INTEGER NOT NULL,scope TEXT NOT NULL,app TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS votes(proposal TEXT NOT NULL,voter TEXT NOT NULL,decision TEXT NOT NULL,PRIMARY KEY(proposal,voter));
CREATE TABLE IF NOT EXISTS handoffs(task TEXT PRIMARY KEY,owner TEXT NOT NULL,assignee TEXT NOT NULL,returns INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skills(id TEXT NOT NULL,version TEXT NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,approval TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS skill_uses(task TEXT NOT NULL,skill TEXT NOT NULL,version TEXT NOT NULL,stage TEXT NOT NULL,PRIMARY KEY(task,skill));
CREATE TABLE IF NOT EXISTS skill_deployments(scope TEXT NOT NULL,skill TEXT NOT NULL,version TEXT NOT NULL,digest TEXT NOT NULL,stage TEXT NOT NULL,PRIMARY KEY(scope,skill));
CREATE TABLE IF NOT EXISTS task_reads(task TEXT NOT NULL,document TEXT NOT NULL,PRIMARY KEY(task,document));
CREATE TABLE IF NOT EXISTS executions(task TEXT PRIMARY KEY,job_path TEXT NOT NULL,output_dir TEXT NOT NULL,state TEXT NOT NULL,started TEXT NOT NULL,finished TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS task_links(child TEXT PRIMARY KEY,parent TEXT NOT NULL,kind TEXT NOT NULL,request_document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_requests(owner_task TEXT NOT NULL,id TEXT NOT NULL,child TEXT NOT NULL,request_document TEXT NOT NULL,resume_task TEXT,PRIMARY KEY(owner_task,id));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT NOT NULL,actor TEXT NOT NULL,task TEXT,action TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wechat_imports(document TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_digest TEXT NOT NULL,approval TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wechat_captures(id TEXT PRIMARY KEY,task TEXT NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,document TEXT,digest TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wechat_requests(id TEXT PRIMARY KEY,actor TEXT NOT NULL,scope TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,created TEXT NOT NULL,approval TEXT);
CREATE TABLE IF NOT EXISTS report_artifacts(document TEXT PRIMARY KEY,source TEXT NOT NULL,source_revision INTEGER NOT NULL,source_digest TEXT NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS file_replies(reply_document TEXT PRIMARY KEY,report_document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS citation_batches(id TEXT PRIMARY KEY,task TEXT NOT NULL,event TEXT NOT NULL,body TEXT NOT NULL,digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS citation_batches_task ON citation_batches(task);
CREATE INDEX IF NOT EXISTS audit_action_id ON audit(action,id);
CREATE INDEX IF NOT EXISTS audit_task_action_id ON audit(task,action,id);
CREATE INDEX IF NOT EXISTS commits_document_revision ON commits(document,revision);
CREATE INDEX IF NOT EXISTS commits_task ON commits(task);
CREATE TABLE IF NOT EXISTS materials(document TEXT PRIMARY KEY,source TEXT NOT NULL,part TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_publications(id TEXT PRIMARY KEY,task TEXT NOT NULL,scope TEXT NOT NULL,report TEXT NOT NULL,format TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_copies(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_publications(id TEXT PRIMARY KEY,scope TEXT NOT NULL,report TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_requests(id TEXT PRIMARY KEY,task TEXT,scope TEXT,actor TEXT,spec TEXT,state TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS delivery_retries(outbox TEXT PRIMARY KEY,attempts INTEGER NOT NULL,next_attempt REAL NOT NULL);
CREATE TABLE IF NOT EXISTS group_settings(scope TEXT PRIMARY KEY,alias TEXT,intro_done INTEGER NOT NULL DEFAULT 0,set_by TEXT,set_at TEXT,intro_task TEXT);
CREATE TABLE IF NOT EXISTS member_labels(member TEXT PRIMARY KEY,display_name TEXT,display_source TEXT,call_name TEXT,about TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS member_settings(member TEXT PRIMARY KEY,model TEXT,reasoning TEXT,updated TEXT,updated_by TEXT);
CREATE TABLE IF NOT EXISTS usage_log(task TEXT PRIMARY KEY,scope TEXT,actor TEXT,kind TEXT,model TEXT,effort TEXT,input_tokens INTEGER,cached_tokens INTEGER,output_tokens INTEGER,reasoning_tokens INTEGER,tool_calls INTEGER,started TEXT,finished TEXT,recorded TEXT);
CREATE INDEX IF NOT EXISTS usage_log_finished ON usage_log(finished);
CREATE TABLE IF NOT EXISTS quota_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,account TEXT,scope TEXT,task TEXT,window_minutes INTEGER,used_percent REAL,resets_at INTEGER,has_credits INTEGER,observed TEXT,observed_source TEXT,recorded TEXT);
CREATE TABLE IF NOT EXISTS member_preferences(member TEXT PRIMARY KEY,text TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS share_requests(id TEXT PRIMARY KEY,actor TEXT NOT NULL,scope TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,created TEXT NOT NULL,approval TEXT);
'''


# Schema evolution.  SCHEMA above always describes the newest layout.  To change a table, edit SCHEMA and add
# MIGRATIONS[n] = 'SQL turning version n into n+1' (n starts at 1).  An existing database is copied to
# '<name>.pre-v<n>' and migrated in one transaction at open; a failure leaves the original untouched.
MIGRATIONS = {
    6: 'CREATE TABLE IF NOT EXISTS share_requests(id TEXT PRIMARY KEY,actor TEXT NOT NULL,scope TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,created TEXT NOT NULL,approval TEXT)',
    5: 'CREATE TABLE IF NOT EXISTS member_preferences(member TEXT PRIMARY KEY,text TEXT NOT NULL,updated TEXT NOT NULL)',
    4: ('CREATE TABLE IF NOT EXISTS usage_log(task TEXT PRIMARY KEY,scope TEXT,actor TEXT,kind TEXT,model TEXT,effort TEXT,input_tokens INTEGER,cached_tokens INTEGER,'
        'output_tokens INTEGER,reasoning_tokens INTEGER,tool_calls INTEGER,started TEXT,finished TEXT,recorded TEXT);'
        'CREATE INDEX IF NOT EXISTS usage_log_finished ON usage_log(finished);'
        'CREATE TABLE IF NOT EXISTS quota_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,account TEXT,scope TEXT,task TEXT,window_minutes INTEGER,used_percent REAL,resets_at INTEGER,'
        'has_credits INTEGER,observed TEXT,observed_source TEXT,recorded TEXT)'),
    3: 'CREATE TABLE IF NOT EXISTS member_settings(member TEXT PRIMARY KEY,model TEXT,reasoning TEXT,updated TEXT,updated_by TEXT)',
    2: 'CREATE TABLE IF NOT EXISTS member_labels(member TEXT PRIMARY KEY,display_name TEXT,display_source TEXT,call_name TEXT,about TEXT,updated TEXT)',
    1: 'CREATE TABLE IF NOT EXISTS group_settings(scope TEXT PRIMARY KEY,alias TEXT,intro_done INTEGER NOT NULL DEFAULT 0,set_by TEXT,set_at TEXT,intro_task TEXT)',
}
LATEST = 1 + len(MIGRATIONS)
# DOWN[n] undoes MIGRATIONS[n] (version n+1 -> n) so a deployment that fails after the migration can put the OLD code back
# together with a database it accepts.  Each step must lose only what the migration itself introduced.
DOWN = {
    6: 'DROP TABLE IF EXISTS share_requests',
    5: 'DROP TABLE IF EXISTS member_preferences',
    4: 'DROP TABLE IF EXISTS quota_snapshots;DROP TABLE IF EXISTS usage_log',
    3: 'DROP TABLE IF EXISTS member_settings',
    2: 'DROP TABLE IF EXISTS member_labels',
    1: 'DROP TABLE IF EXISTS group_settings',
}


BUSY = {
    'tasks': "SELECT COUNT(*) FROM tasks WHERE state IN ('queued','preparing','running','ingesting')",
    'executions': 'SELECT COUNT(*) FROM executions WHERE finished IS NULL',
    'replies': "SELECT COUNT(*) FROM outbox WHERE state IN ('queued','sending')",
}


def busy(db):
    """What must finish before an administrator stops services, switches methods or changes the roster: unfinished
    tasks, model executions that have not ended, and replies queued or being sent."""
    return {name: db.execute(sql).fetchone()[0] for name, sql in BUSY.items()}


def downgrade(path, to_version, down=None):
    """Undo migrations until `to_version`; the caller has already saved a full copy of the database."""
    down = DOWN if down is None else down
    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=15)) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version < to_version:
            raise RuntimeError('Database is older than the requested version')
        if version == to_version:
            return version
        db.executescript('BEGIN IMMEDIATE;\n' + ''.join(down[n] + ';\nPRAGMA user_version=%d;\n' % n
                                                        for n in range(version - 1, to_version - 1, -1)) + 'COMMIT;')
        return to_version


def migrate(path, db, version, migrations=None):
    migrations = MIGRATIONS if migrations is None else migrations
    latest = 1 + len(migrations)
    if version < 1 or version >= latest:
        return version
    backup = Path(str(path) + '.pre-v%d' % version)
    if not backup.exists():
        with contextlib.closing(sqlite3.connect(backup)) as copy:
            db.backup(copy)
    db.executescript('BEGIN IMMEDIATE;\n' + ''.join(migrations[n] + ';\n' + 'PRAGMA user_version=%d;\n' % (n + 1)
                                                  for n in range(version, latest)) + 'COMMIT;')
    return latest


class Connection(sqlite3.Connection):
    """Per-transaction receipts, never a second durable state store."""


class Database:
    def __init__(self, path):
        self.after_commit = None
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version < 0 or version > LATEST:
                raise RuntimeError('Database schema is newer or unsupported; preserve records and stop')
            version = migrate(self.path, db, version)
            with contextlib.closing(sqlite3.connect(':memory:')) as expected:
                expected.executescript(SCHEMA)
                names = {r[0] for r in expected.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                    name = row[0]
                    if name not in names:
                        raise RuntimeError('Unrecognized database; old product databases are not migration inputs')
                    before = [tuple(r) for r in db.execute('PRAGMA table_info("' + name + '")')]
                    after = [tuple(r) for r in expected.execute('PRAGMA table_info("' + name + '")')]
                    if before != after:
                        raise RuntimeError('Database schema differs; explicit compatible migration required')
            db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nPRAGMA user_version=%d;\nCOMMIT;' % LATEST)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None, factory=Connection)
        db.action_receipts = []
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextlib.contextmanager
    def keep_open(self):
        """Avoid a final-close WAL checkpoint after every short transaction.

        This connection never starts a transaction or serves requests. Writers
        keep independent FULL-synchronous connections and automatic checkpoints.
        Older SQLite deployments retain the existing lifetime until upgraded;
        require the upstream WAL-reset fix before enabling this optimization.
        """
        if sqlite3.sqlite_version_info < (3, 51, 3):
            yield False
            return
        with contextlib.closing(self.connect()):
            yield True

    @contextlib.contextmanager
    def transaction(self):
        with contextlib.closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.execute('ROLLBACK')
                raise
            else:
                db.execute('COMMIT')
                receipts = db.action_receipts
        # SQL is already committed. A failed mirror must not pretend rollback;
        # the caller stops, while recovery can rebuild from the exact audit.
        if receipts and self.after_commit:
            self.after_commit(receipts)

    def get_policy(self, db):
        row = db.execute('SELECT body FROM policy WHERE singleton=1').fetchone()
        if not row:
            raise RuntimeError('Policy not initialized')
        return json.loads(row[0])
