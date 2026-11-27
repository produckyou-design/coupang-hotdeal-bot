const ALLOWED_HOSTS = new Set([
  "link.coupang.com",
  "www.coupang.com",
  "m.coupang.com",
  "coupang.com",
  "coupa.ng",
]);

const BOT_UA_RE =
  /telegrambot|twitterbot|facebookexternalhit|slackbot|discordbot|whatsapp|kakaotalk-scrap|googlebot|bingbot|yeti|daum|curl\/|python-requests|headlesschrome|bot|crawler|spider|preview|uptime|monitor/i;

const MOBILE_UA_RE = /android|iphone|ipad|ipod|mobile/i;
const CODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
const CHAT_ID_RE = /^-?\d{3,20}$/;
const MAX_KEYWORDS_PER_USER = 10;

export function normalizeAlertKeyword(raw) {
  return String(raw || "").trim().replace(/\s+/g, "").toLowerCase();
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json;charset=UTF-8",
      "Cache-Control": "no-store",
    },
  });
}

function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function isAuthorized(request, env) {
  const expected = String((env && env.CLICK_TRACKER_API_KEY) || "").trim();
  if (!expected) {
    return false;
  }
  const authHeader = request.headers.get("Authorization") || "";
  if (!authHeader.startsWith("Bearer ")) {
    return false;
  }
  const provided = authHeader.slice(7).trim();
  return constantTimeEqual(provided, expected);
}

export function isAllowedCoupangUrl(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || "").trim());
    if (parsed.protocol !== "https:") {
      return false;
    }
    return ALLOWED_HOSTS.has(parsed.hostname.toLowerCase());
  } catch {
    return false;
  }
}

export function isBotRequest(request) {
  if (!request) {
    return true;
  }
  if (String(request.method || "").toUpperCase() === "HEAD") {
    return true;
  }
  const purpose = `${request.headers.get("purpose") || ""} ${request.headers.get("sec-purpose") || ""}`.toLowerCase();
  if (purpose.includes("prefetch")) {
    return true;
  }
  const ua = (request.headers.get("user-agent") || "").trim();
  if (!ua) {
    return true;
  }
  return BOT_UA_RE.test(ua);
}

export function toKstParts(dateInput = new Date()) {
  const kstMs = dateInput.getTime() + 9 * 3600 * 1000;
  const kst = new Date(kstMs);
  const year = kst.getUTCFullYear();
  const month = String(kst.getUTCMonth() + 1).padStart(2, "0");
  const day = String(kst.getUTCDate()).padStart(2, "0");
  return {
    kstDate: `${year}-${month}-${day}`,
    kstHour: kst.getUTCHours(),
  };
}

function generateCode(length = 8) {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  let out = "";
  for (let i = 0; i < length; i += 1) {
    out += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
  }
  return out;
}

function clampInt(raw, fallback, minVal, maxVal) {
  const parsed = Number.parseInt(String(raw ?? ""), 10);
  if (Number.isNaN(parsed)) {
    return fallback;
  }
  return Math.max(minVal, Math.min(maxVal, parsed));
}

async function handleCreateLink(request, env, origin) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }

  const targetUrl = String(body?.target_url || "").trim();
  if (!isAllowedCoupangUrl(targetUrl)) {
    return jsonResponse({ ok: false, error: "invalid_target_url" }, 400);
  }

  const dealId = String(body?.deal_id || "").trim().slice(0, 120);
  const productId = String(body?.product_id || "").trim().slice(0, 64);
  const source = String(body?.source || "").trim().slice(0, 32);
  const track = String(body?.track || "A").trim().slice(0, 8) || "A";
  const title = String(body?.title || "").trim().slice(0, 200);
  const salePrice = Math.max(0, Number.parseInt(String(body?.sale_price ?? 0), 10) || 0);
  const discountRate = Math.max(0, Number.parseInt(String(body?.discount_rate ?? 0), 10) || 0);

  if (dealId) {
    const existing = await env.DB.prepare(
      "SELECT code FROM links WHERE deal_id = ? AND target_url = ? LIMIT 1"
    )
      .bind(dealId, targetUrl)
      .first();
    if (existing && existing.code) {
      return jsonResponse({
        ok: true,
        code: existing.code,
        short_url: `${origin}/r/${existing.code}`,
        target_url: targetUrl,
      });
    }
  }

  const createdAt = new Date().toISOString();
  let code = "";
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const candidate = generateCode(8);
    try {
      await env.DB.prepare(
        `INSERT INTO links (
          code, target_url, deal_id, product_id, source, track,
          title, sale_price, discount_rate, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(candidate, targetUrl, dealId, productId, source, track, title, salePrice, discountRate, createdAt)
        .run();
      code = candidate;
      break;
    } catch {
      // Retry on rare 8-char collision
    }
  }

  if (!code) {
    return jsonResponse({ ok: false, error: "code_generation_failed" }, 500);
  }

  return jsonResponse({
    ok: true,
    code,
    short_url: `${origin}/r/${code}`,
    target_url: targetUrl,
  });
}

async function handleRedirect(request, env, ctx, code) {
  if (!code || code.length > 32) {
    return jsonResponse({ ok: false, error: "not_found" }, 404);
  }

  const row = await env.DB.prepare("SELECT target_url FROM links WHERE code = ? LIMIT 1")
    .bind(code)
    .first();
  if (!row || !row.target_url) {
    return jsonResponse({ ok: false, error: "not_found" }, 404);
  }

  if (!isAllowedCoupangUrl(row.target_url)) {
    return jsonResponse({ ok: false, error: "invalid_target" }, 400);
  }

  if (!isBotRequest(request)) {
    const now = new Date();
    const { kstDate, kstHour } = toKstParts(now);
    const ua = (request.headers.get("user-agent") || "").trim();
    const isMobile = MOBILE_UA_RE.test(ua) ? 1 : 0;
    const country = String(request.cf?.country || "").slice(0, 8);
    const uaShort = ua.slice(0, 80);

    const writePromise = env.DB.prepare(
      `INSERT INTO clicks (code, clicked_at, kst_date, kst_hour, is_mobile, country, ua_short)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(code, now.toISOString(), kstDate, kstHour, isMobile, country, uaShort)
      .run()
      .catch(() => {});

    if (ctx && typeof ctx.waitUntil === "function") {
      ctx.waitUntil(writePromise);
    } else {
      await writePromise;
    }
  }

  return new Response(null, {
    status: 302,
    headers: {
      Location: row.target_url,
      "Cache-Control": "no-store, no-cache, must-revalidate",
      "Referrer-Policy": "no-referrer",
    },
  });
}

async function handleStats(request, env, url) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }

  const days = clampInt(url.searchParams.get("days"), 7, 1, 30);
  const limit = clampInt(url.searchParams.get("limit"), 10, 1, 30);

  const now = new Date();
  const todayKst = toKstParts(now).kstDate;
  const startKst = toKstParts(new Date(now.getTime() - (days - 1) * 86400 * 1000)).kstDate;

  const todaySummary = await env.DB.prepare(
    "SELECT COUNT(*) AS clicks, COUNT(DISTINCT code) AS unique_links FROM clicks WHERE kst_date = ?"
  )
    .bind(todayKst)
    .first();

  const periodClicksRow = await env.DB.prepare(
    "SELECT COUNT(*) AS clicks FROM clicks WHERE kst_date >= ?"
  )
    .bind(startKst)
    .first();

  const totalLinksRow = await env.DB.prepare("SELECT COUNT(*) AS total_links FROM links").first();

  const byDateRes = await env.DB.prepare(
    "SELECT kst_date AS date, COUNT(*) AS clicks FROM clicks WHERE kst_date >= ? GROUP BY kst_date ORDER BY kst_date DESC"
  )
    .bind(startKst)
    .all();

  const bySourceTodayRes = await env.DB.prepare(
    `SELECT COALESCE(NULLIF(l.source, ''), 'unknown') AS source, COUNT(*) AS clicks
     FROM clicks c JOIN links l ON c.code = l.code
     WHERE c.kst_date = ?
     GROUP BY COALESCE(NULLIF(l.source, ''), 'unknown')
     ORDER BY clicks DESC`
  )
    .bind(todayKst)
    .all();

  const bySourcePeriodRes = await env.DB.prepare(
    `SELECT COALESCE(NULLIF(l.source, ''), 'unknown') AS source, COUNT(*) AS clicks
     FROM clicks c JOIN links l ON c.code = l.code
     WHERE c.kst_date >= ?
     GROUP BY COALESCE(NULLIF(l.source, ''), 'unknown')
     ORDER BY clicks DESC`
  )
    .bind(startKst)
    .all();

  const topTodayRes = await env.DB.prepare(
    `SELECT l.code, l.title, l.source, l.sale_price, l.discount_rate, COUNT(*) AS clicks
     FROM clicks c JOIN links l ON c.code = l.code
     WHERE c.kst_date = ?
     GROUP BY l.code, l.title, l.source, l.sale_price, l.discount_rate
     ORDER BY clicks DESC, MAX(c.id) DESC
     LIMIT ?`
  )
    .bind(todayKst, limit)
    .all();

  const topPeriodRes = await env.DB.prepare(
    `SELECT l.code, l.title, l.source, l.sale_price, l.discount_rate, COUNT(*) AS clicks
     FROM clicks c JOIN links l ON c.code = l.code
     WHERE c.kst_date >= ?
     GROUP BY l.code, l.title, l.source, l.sale_price, l.discount_rate
     ORDER BY clicks DESC, MAX(c.id) DESC
     LIMIT ?`
  )
    .bind(startKst, limit)
    .all();

  const recentRes = await env.DB.prepare(
    `SELECT c.clicked_at, l.code, l.title, l.source, l.sale_price, l.discount_rate, c.is_mobile
     FROM clicks c JOIN links l ON c.code = l.code
     ORDER BY c.id DESC
     LIMIT 10`
  ).all();

  return jsonResponse({
    ok: true,
    today_kst: todayKst,
    start_kst: startKst,
    days,
    today: {
      clicks: Number(todaySummary?.clicks || 0),
      unique_links: Number(todaySummary?.unique_links || 0),
    },
    period_total: {
      clicks: Number(periodClicksRow?.clicks || 0),
      total_links: Number(totalLinksRow?.total_links || 0),
    },
    by_date: byDateRes?.results || [],
    by_source_today: bySourceTodayRes?.results || [],
    by_source_period: bySourcePeriodRes?.results || [],
    top_today: topTodayRes?.results || [],
    top_period: topPeriodRes?.results || [],
    recent: recentRes?.results || [],
  });
}

async function handleGetAlerts(request, env, url) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const chatId = String(url.searchParams.get("chat_id") || "").trim();
  if (chatId) {
    if (!CHAT_ID_RE.test(chatId)) {
      return jsonResponse({ ok: false, error: "invalid_chat_id" }, 400);
    }
    const res = await env.DB.prepare(
      "SELECT id, chat_id, keyword, keyword_norm, created_at FROM keyword_alerts WHERE chat_id = ? ORDER BY id ASC LIMIT ?"
    )
      .bind(chatId, MAX_KEYWORDS_PER_USER)
      .all();
    return jsonResponse({ ok: true, alerts: res?.results || [] });
  }
  const res = await env.DB.prepare(
    "SELECT id, chat_id, keyword, keyword_norm, created_at FROM keyword_alerts ORDER BY id ASC LIMIT 2000"
  ).all();
  return jsonResponse({ ok: true, alerts: res?.results || [] });
}

async function handleCreateAlert(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const chatId = String(body?.chat_id ?? "").trim();
  if (!CHAT_ID_RE.test(chatId)) {
    return jsonResponse({ ok: false, error: "invalid_chat_id" }, 400);
  }
  const keyword = String(body?.keyword || "").trim().replace(/\s+/g, " ");
  const keywordNorm = normalizeAlertKeyword(keyword);
  if (keyword.length < 2 || keyword.length > 24 || keywordNorm.length < 2) {
    return jsonResponse({ ok: false, error: "invalid_keyword" }, 400);
  }

  const existing = await env.DB.prepare(
    "SELECT keyword FROM keyword_alerts WHERE chat_id = ? AND keyword_norm = ? LIMIT 1"
  )
    .bind(chatId, keywordNorm)
    .first();
  if (existing && existing.keyword) {
    const cntRow = await env.DB.prepare(
      "SELECT COUNT(*) AS cnt FROM keyword_alerts WHERE chat_id = ?"
    )
      .bind(chatId)
      .first();
    return jsonResponse({
      ok: true,
      keyword: existing.keyword,
      keyword_norm: keywordNorm,
      count: Number(cntRow?.cnt || 1),
      already_exists: true,
    });
  }

  const cntRow = await env.DB.prepare(
    "SELECT COUNT(*) AS cnt FROM keyword_alerts WHERE chat_id = ?"
  )
    .bind(chatId)
    .first();
  const currentCnt = Number(cntRow?.cnt || 0);
  if (currentCnt >= MAX_KEYWORDS_PER_USER) {
    return jsonResponse(
      { ok: false, error: "limit_exceeded", max_keywords: MAX_KEYWORDS_PER_USER },
      400
    );
  }

  const createdAt = new Date().toISOString();
  await env.DB.prepare(
    "INSERT OR IGNORE INTO keyword_alerts (chat_id, keyword, keyword_norm, created_at) VALUES (?, ?, ?, ?)"
  )
    .bind(chatId, keyword, keywordNorm, createdAt)
    .run();

  return jsonResponse({
    ok: true,
    keyword,
    keyword_norm: keywordNorm,
    count: currentCnt + 1,
    already_exists: false,
  });
}

async function handleDeleteAlert(request, env, url) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const chatId = String(url.searchParams.get("chat_id") || "").trim();
  if (!CHAT_ID_RE.test(chatId)) {
    return jsonResponse({ ok: false, error: "invalid_chat_id" }, 400);
  }
  const delAll = url.searchParams.get("all") === "1";
  if (delAll) {
    const info = await env.DB.prepare("DELETE FROM keyword_alerts WHERE chat_id = ?")
      .bind(chatId)
      .run();
    return jsonResponse({ ok: true, deleted: Number(info?.meta?.changes ?? 0) });
  }
  const keywordNorm = normalizeAlertKeyword(url.searchParams.get("keyword") || "");
  if (!keywordNorm) {
    return jsonResponse({ ok: false, error: "invalid_keyword" }, 400);
  }
  const info = await env.DB.prepare(
    "DELETE FROM keyword_alerts WHERE chat_id = ? AND keyword_norm = ?"
  )
    .bind(chatId, keywordNorm)
    .run();
  return jsonResponse({ ok: true, deleted: Number(info?.meta?.changes ?? 0) });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const method = String(request.method || "GET").toUpperCase();
    const path = url.pathname;

    if (method === "GET" && path === "/health") {
      return jsonResponse({ ok: true, service: "hotpang-click" });
    }
    if (method === "POST" && path === "/api/links") {
      return handleCreateLink(request, env, url.origin);
    }
    if (method === "GET" && path === "/api/stats") {
      return handleStats(request, env, url);
    }
    if (path === "/api/alerts") {
      if (method === "GET") return handleGetAlerts(request, env, url);
      if (method === "POST") return handleCreateAlert(request, env);
      if (method === "DELETE") return handleDeleteAlert(request, env, url);
    }
    if ((method === "GET" || method === "HEAD") && path.startsWith("/r/")) {
      const code = path.slice(3).split("/")[0];
      return handleRedirect(request, env, ctx, code);
    }

    return jsonResponse({ ok: false, error: "not_found" }, 404);
  },
};
