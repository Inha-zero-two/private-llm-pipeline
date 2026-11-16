-- ChatManager가 빈 DB를 만들 때 실행하는 SQLite 스키마.
-- 스키마 버전은 관리하지 않는다. 이 파일을 바꾸면 기존 DB 파일
-- (chat.sqlite3와 -wal·-shm 보조 파일)을 지우고 다시 실행한다.
-- 컬럼 설명은 docs/db-schema.md에 있으며, 둘이 다르면 이 파일이 기준이다.

CREATE TABLE chats (
    chatId INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '',
    createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL
);
CREATE TABLE messages (
    messageId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, createdAt TEXT NOT NULL
);
CREATE TABLE privacy_settings (
    settingId INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
    rules TEXT NOT NULL, updatedAt TEXT NOT NULL
);
CREATE TABLE pd_items (
    pdId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    messageId INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
    text TEXT NOT NULL, category INTEGER NOT NULL CHECK(category BETWEEN 0 AND 2),
    allowed INTEGER NOT NULL DEFAULT 0 CHECK(allowed IN (0, 1)),
    allowedBy TEXT NOT NULL DEFAULT 'none', createdAt TEXT NOT NULL,
    UNIQUE(chatId, text, category)
);
CREATE TABLE runs (
    runId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    userMessageId INTEGER NOT NULL REFERENCES messages,
    answerMessageId INTEGER REFERENCES messages,
    settingId INTEGER REFERENCES privacy_settings,
    config TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running', 'finished')),
    endReason TEXT, notice TEXT, errorCode TEXT,
    frontierCalls INTEGER NOT NULL DEFAULT 0,
    regenerations INTEGER NOT NULL DEFAULT 0, spentCost REAL NOT NULL DEFAULT 0,
    memoFinal TEXT, startedAt TEXT NOT NULL, endedAt TEXT
);
CREATE TABLE run_events (
    eventId INTEGER NOT NULL,
    runId INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    type TEXT NOT NULL, payload TEXT NOT NULL, createdAt TEXT NOT NULL,
    PRIMARY KEY(runId, eventId)
);
CREATE TABLE consent_requests (
    consentId INTEGER PRIMARY KEY AUTOINCREMENT,
    runId INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    items TEXT NOT NULL, purpose TEXT NOT NULL, benefit TEXT NOT NULL,
    risk TEXT NOT NULL, result TEXT CHECK(result IN ('answered', 'cancelled')),
    responseText TEXT, decisions TEXT NOT NULL DEFAULT '[]',
    createdAt TEXT NOT NULL, respondedAt TEXT
);
CREATE UNIQUE INDEX pending_consent ON consent_requests(runId) WHERE result IS NULL;
CREATE TABLE frontier_queries (
    queryId INTEGER PRIMARY KEY AUTOINCREMENT,
    runId INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    round INTEGER NOT NULL, request TEXT NOT NULL, answer TEXT,
    status TEXT NOT NULL CHECK(status IN ('ok', 'failed', 'uncertain')),
    contextMode TEXT, inputTokens INTEGER, outputTokens INTEGER,
    cost REAL NOT NULL DEFAULT 0, elapsedMs REAL, createdAt TEXT NOT NULL,
    UNIQUE(runId, round)
);
CREATE TABLE query_items (
    itemId INTEGER PRIMARY KEY AUTOINCREMENT,
    queryId INTEGER NOT NULL REFERENCES frontier_queries ON DELETE CASCADE,
    pdId INTEGER NOT NULL REFERENCES pd_items ON DELETE CASCADE,
    allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)), allowedBy TEXT NOT NULL,
    UNIQUE(queryId, pdId)
);
CREATE INDEX messages_chat ON messages(chatId, messageId);
CREATE INDEX runs_chat ON runs(chatId, runId);
CREATE INDEX events_run ON run_events(runId, eventId);
CREATE INDEX consents_run ON consent_requests(runId, consentId);
CREATE INDEX queries_run ON frontier_queries(runId, round);
