-- ChatManager가 빈 DB를 만들 때 실행하는 SQLite 스키마.
-- 기존 DB는 삭제하지 않는다. ChatManager가 user_version별로 트랜잭션 갱신한다.
-- 2→3 갱신은 아래 Context persistence 구조와 기존 메시지 연결을 함께 적용한다.
-- 3→4 갱신은 맨 뒤의 비교 기능 구역만 적용한다. 구역 표시 주석은 한 번씩만 쓴다.
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
    text TEXT NOT NULL, category INTEGER NOT NULL CHECK(category BETWEEN 0 AND 3),
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
    endReason TEXT, notice TEXT, errorCode TEXT, errorStage TEXT,
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
-- Context persistence
CREATE TABLE context_entries (
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    contextKey TEXT NOT NULL,
    contextSeq INTEGER NOT NULL CHECK(contextSeq > 0),
    messageId INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
    PRIMARY KEY(chatId, contextKey, contextSeq),
    UNIQUE(chatId, contextKey, messageId)
);
CREATE TABLE context_compactions (
    compactionId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    contextKey TEXT NOT NULL,
    coveredThroughSeq INTEGER NOT NULL CHECK(coveredThroughSeq > 0),
    summary TEXT NOT NULL CHECK(length(trim(summary)) > 0),
    runId INTEGER REFERENCES runs ON DELETE SET NULL,
    createdAt TEXT NOT NULL,
    FOREIGN KEY(chatId, contextKey, coveredThroughSeq)
        REFERENCES context_entries(chatId, contextKey, contextSeq) ON DELETE CASCADE
);
CREATE INDEX compactions_chat ON context_compactions(chatId, contextKey, compactionId);
PRAGMA user_version = 3;
-- Comparison
CREATE TABLE comparison_chats (
    chatId INTEGER PRIMARY KEY REFERENCES chats ON DELETE CASCADE
);
CREATE TABLE comparisons (
    comparisonId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    runId INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('local', 'frontier')),
    question TEXT NOT NULL, answer TEXT,
    status TEXT NOT NULL
        CHECK(status IN ('running', 'ok', 'truncated', 'failed', 'stopped')),
    errorCode TEXT, modelName TEXT,
    attempts INTEGER NOT NULL DEFAULT 1, droppedPairs INTEGER NOT NULL DEFAULT 0,
    inputTokens INTEGER, outputTokens INTEGER, cost REAL, elapsedMs REAL,
    createdAt TEXT NOT NULL, endedAt TEXT,
    UNIQUE(runId, kind)
);
CREATE INDEX comparisons_history ON comparisons(chatId, kind, runId);
PRAGMA user_version = 4;
