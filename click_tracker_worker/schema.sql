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

CREATE INDEX IF NOT EXISTS idx_links_deal_id ON links(deal_id);
CREATE INDEX IF NOT EXISTS idx_links_created ON links(created_at);
CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);
CREATE INDEX IF NOT EXISTS idx_clicks_kst_date ON clicks(kst_date);
