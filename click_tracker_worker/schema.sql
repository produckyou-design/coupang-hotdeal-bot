CREATE TABLE IF NOT EXISTS links (
  code TEXT PRIMARY KEY,
  target_url TEXT NOT NULL,
  deal_id TEXT DEFAULT '',
  product_id TEXT DEFAULT '',
  source TEXT DEFAULT '',
  track TEXT DEFAULT 'A',
  title TEXT DEFAULT '',
  sale_price INTEGER DEFAULT 0,
  discount_rate INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clicks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  clicked_at TEXT NOT NULL,
  kst_date TEXT NOT NULL,
  kst_hour INTEGER NOT NULL,
  is_mobile INTEGER DEFAULT 0,
  country TEXT DEFAULT '',
  ua_short TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS keyword_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  keyword TEXT NOT NULL,
  keyword_norm TEXT NOT NULL,
  max_price INTEGER DEFAULT 0,
  min_discount INTEGER DEFAULT 0,
  exclude_terms TEXT DEFAULT '',
  hit_count INTEGER DEFAULT 0,
  last_notified_at TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(chat_id, keyword_norm)
);

CREATE TABLE IF NOT EXISTS alert_prefs (
  chat_id TEXT PRIMARY KEY,
  paused INTEGER DEFAULT 0,
  quiet_night INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_stats (
  kst_date TEXT PRIMARY KEY,
  start_count INTEGER NOT NULL,
  latest_count INTEGER NOT NULL,
  peak_count INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posted_messages (
  message_id INTEGER PRIMARY KEY,
  chat_id TEXT NOT NULL,
  caption TEXT NOT NULL,
  items_json TEXT NOT NULL,
  inline_kb_json TEXT NOT NULL,
  all_closed INTEGER DEFAULT 0,
  posted_at TEXT NOT NULL,
  last_checked_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_links_deal_id ON links(deal_id);
CREATE INDEX IF NOT EXISTS idx_links_created ON links(created_at);
CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);
CREATE INDEX IF NOT EXISTS idx_clicks_kst_date ON clicks(kst_date);
CREATE INDEX IF NOT EXISTS idx_keyword_alerts_chat ON keyword_alerts(chat_id);
CREATE INDEX IF NOT EXISTS idx_keyword_alerts_norm ON keyword_alerts(keyword_norm);
CREATE INDEX IF NOT EXISTS idx_posted_messages_active ON posted_messages(all_closed, posted_at);

CREATE TABLE IF NOT EXISTS bot_sessions (
  chat_id TEXT PRIMARY KEY,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
