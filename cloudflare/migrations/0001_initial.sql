CREATE TABLE accounts(id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, employee_id TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL, department TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','employee')),
 password_hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE options(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), csrf TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE INDEX sessions_account ON sessions(account_id);
CREATE TABLE balances(employee_id TEXT NOT NULL REFERENCES accounts(employee_id), leave_type TEXT NOT NULL CHECK(leave_type IN ('annual','compensatory')), hours REAL NOT NULL CHECK(hours>=0), PRIMARY KEY(employee_id,leave_type));
CREATE TABLE leaves(id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES accounts(employee_id), leave_type TEXT NOT NULL CHECK(leave_type IN ('annual','compensatory')),
 start TEXT NOT NULL, end TEXT NOT NULL, hours REAL NOT NULL CHECK(hours>0), reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending','approved','rejected','cancelled','withdrawn')));
CREATE INDEX leaves_employee ON leaves(employee_id,start);
CREATE INDEX leaves_status ON leaves(status,start);
CREATE UNIQUE INDEX unique_draft ON leaves(employee_id,leave_type,start,end,reason) WHERE status='draft';
CREATE TABLE leave_events(id INTEGER PRIMARY KEY, leave_id TEXT NOT NULL REFERENCES leaves(id), action TEXT NOT NULL, hours REAL NOT NULL, note TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours')));
CREATE INDEX leave_events_leave ON leave_events(leave_id,id);
CREATE TABLE reviews(leave_id TEXT PRIMARY KEY REFERENCES leaves(id), reviewer_id TEXT NOT NULL REFERENCES accounts(id), decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')), note TEXT NOT NULL);
CREATE TABLE calendar(id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES accounts(employee_id), title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, location TEXT NOT NULL DEFAULT '');
CREATE INDEX calendar_employee ON calendar(employee_id,start);
CREATE TABLE conversations(id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES accounts(employee_id), messages TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX conversations_employee ON conversations(employee_id,updated_at);
CREATE TABLE receipts(id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES accounts(employee_id), signature TEXT NOT NULL, response TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE providers(id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, encrypted_key TEXT NOT NULL, key_suffix TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, tested INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE admin_events(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours')));
CREATE TABLE rate_limits(key TEXT PRIMARY KEY, hits INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE locks(key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires INTEGER NOT NULL);

-- Every state mutation, balance change and leave event commits in the same D1 statement.
CREATE TRIGGER leave_draft_event AFTER INSERT ON leaves BEGIN
 INSERT INTO leave_events(leave_id,action,hours) VALUES(NEW.id,'draft_created',NEW.hours);
END;
CREATE TRIGGER leave_transition BEFORE UPDATE OF status ON leaves WHEN NEW.status!=OLD.status BEGIN
 SELECT CASE WHEN NOT ((OLD.status='draft' AND NEW.status IN ('pending','cancelled')) OR (OLD.status='pending' AND NEW.status IN ('approved','rejected','withdrawn'))) THEN RAISE(ABORT,'invalid_transition') END;
 SELECT CASE WHEN NEW.status IN ('pending','withdrawn') AND NEW.start<=strftime('%Y-%m-%dT%H:%M:%S','now','+8 hours') THEN RAISE(ABORT,'leave_started') END;
 SELECT CASE WHEN NEW.status='pending' AND NOT EXISTS(SELECT 1 FROM balances WHERE employee_id=NEW.employee_id AND leave_type=NEW.leave_type AND hours>=NEW.hours) THEN RAISE(ABORT,'insufficient_balance') END;
 SELECT CASE WHEN NEW.status='pending' AND EXISTS(SELECT 1 FROM leaves WHERE employee_id=NEW.employee_id AND id!=NEW.id AND status IN ('pending','approved') AND start<NEW.end AND end>NEW.start) THEN RAISE(ABORT,'leave_overlap') END;
END;
CREATE TRIGGER leave_balance AFTER UPDATE OF status ON leaves WHEN NEW.status!=OLD.status BEGIN
 UPDATE balances SET hours=hours-NEW.hours WHERE employee_id=NEW.employee_id AND leave_type=NEW.leave_type AND NEW.status='pending';
 UPDATE balances SET hours=hours+NEW.hours WHERE employee_id=NEW.employee_id AND leave_type=NEW.leave_type AND NEW.status IN ('withdrawn','rejected');
 INSERT INTO leave_events(leave_id,action,hours,note) VALUES(NEW.id,CASE WHEN NEW.status='pending' THEN 'submitted' ELSE NEW.status END,NEW.hours,coalesce((SELECT note FROM reviews WHERE leave_id=NEW.id),''));
END;
CREATE TRIGGER review_guard BEFORE INSERT ON reviews BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM leaves WHERE id=NEW.leave_id AND status='pending') THEN RAISE(ABORT,'invalid_transition') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM accounts a JOIN leaves l ON l.id=NEW.leave_id WHERE a.id=NEW.reviewer_id AND a.role='admin' AND a.disabled=0 AND a.employee_id!=l.employee_id) THEN RAISE(ABORT,'invalid_reviewer') END;
END;
CREATE TRIGGER review_apply AFTER INSERT ON reviews BEGIN
 UPDATE leaves SET status=NEW.decision WHERE id=NEW.leave_id;
END;
