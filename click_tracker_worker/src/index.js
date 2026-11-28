import { handleTelegramUpdate, parseAdminChatId, tgApi } from "./telegram_bot.js";

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
const MEMBER_MILESTONES = [50, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000];
const SOURCE_KR_MAP = {
  arca: "아카라이브",
  ppomppu: "뽐뿌",
  ruliweb: "루리웹",
  dealbada: "딜바다",
  clien: "클리앙",
  goldbox: "골드박스",
};

function publicCorsHeaders(cacheControl = "public, max-age=120") {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": cacheControl,
  };
}

function publicJsonResponse(payload, status = 200, cacheControl = "public, max-age=120") {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json;charset=UTF-8",
      ...publicCorsHeaders(cacheControl),
    },
  });
}

const SYMMETRIC_ALIAS_GROUPS = [
  ["에어팟", "airpods", "airpod"],
  ["아이폰", "iphone"],
  ["아이패드", "ipad"],
  ["맥북", "macbook"],
  ["애플워치", "applewatch"],
  ["갤럭시", "galaxy"],
  ["버즈", "buds"],
  ["소니", "sony"],
  ["다이슨", "dyson"],
  ["로지텍", "logitech"],
  ["닌텐도", "nintendo"],
  ["플스", "플레이스테이션", "ps5", "playstation"],
  ["코카콜라", "cocacola"],
  ["펩시", "pepsi"],
  ["스타벅스", "starbucks"],
  ["네스프레소", "nespresso"],
  ["나이키", "nike"],
  ["아디다스", "adidas"],
  ["뉴발란스", "newbalance"],
  ["필립스", "philips"],
  ["브라운", "braun"],
  ["레고", "lego"],
  ["샤오미", "xiaomi"],
  ["앤커", "anker"],
];

const ONE_WAY_CATEGORY_EXPANSIONS = {
  생수: ["삼다수", "백산수", "아이시스", "탐사수", "몽베스트", "스파클", "평창수", "에비앙", "동원샘물"],
  즉석밥: ["햇반", "오뚜기밥", "비비고밥"],
  물티슈: ["베베숲", "브라운물티슈", "페넬로페", "순둥이", "도담이", "탐사물티슈"],
  기저귀: ["하기스", "팸퍼스", "마미포코", "보솜이"],
  화장지: ["휴지", "롤휴지", "두루마리", "크리넥스", "깨끗한나라", "잘풀리는집", "코디"],
  휴지: ["화장지", "롤휴지", "두루마리", "크리넥스", "깨끗한나라", "잘풀리는집", "코디"],
  제로콜라: ["코카콜라제로", "펩시제로"],
  프로틴: ["단백질", "셀렉스", "마이프로틴", "신타6", "옵티멈"],
  단백질: ["프로틴", "셀렉스", "마이프로틴", "신타6", "옵티멈"],
};

const TOKEN_VARIANT_MAP = (() => {
  const map = new Map();
  for (const group of SYMMETRIC_ALIAS_GROUPS) {
    const norms = group.map((g) => normalizeAlertKeyword(g)).filter(Boolean);
    for (const k of norms) {
      map.set(k, Array.from(new Set([...(map.get(k) || [k]), ...norms])));
    }
  }
  for (const [key, extras] of Object.entries(ONE_WAY_CATEGORY_EXPANSIONS)) {
    const kNorm = normalizeAlertKeyword(key);
    const eNorms = extras.map((g) => normalizeAlertKeyword(g)).filter(Boolean);
    map.set(kNorm, Array.from(new Set([...(map.get(kNorm) || [kNorm]), ...eNorms])));
  }
  return map;
})();

export function normalizeAlertKeyword(raw) {
  return String(raw || "").trim().replace(/\s+/g, "").toLowerCase();
}

function sanitizeExcludeTerms(raw) {
  const parts = Array.isArray(raw) ? raw : String(raw || "").split(",");
  const seen = new Set();
  const clean = [];
  for (const p of parts) {
    const t = String(p || "").trim().replace(/^-+/, "").replace(/\s+/g, " ");
    const n = normalizeAlertKeyword(t);
    if (!n || seen.has(n)) continue;
    seen.add(n);
    clean.push(t.slice(0, 20));
    if (clean.length >= 8) break;
  }
  return clean.join(", ").slice(0, 120);
}

export function matchLinkAgainstRule(linkRow, rule) {
  if (Number(rule?.paused || 0) === 1) return false;
  const titleNorm = normalizeAlertKeyword(linkRow?.title || "");
  const pidNorm = String(linkRow?.product_id || "").trim().toLowerCase();
  const rawKw = String(rule?.keyword || "").trim();
  const kwNorm = normalizeAlertKeyword(rule?.keyword_norm || rawKw);
  if (!kwNorm) return false;

  if (kwNorm.startsWith("pid:")) {
    const targetPid = kwNorm.slice(4).trim();
    if (!pidNorm || !targetPid || pidNorm !== targetPid) return false;
  } else {
    if (!titleNorm) return false;
    const tokens = (rawKw ? rawKw.split(/\s+/) : [String(rule?.keyword_norm || "")])
      .map((t) => normalizeAlertKeyword(t))
      .filter(Boolean);
    if (tokens.length === 0) return false;

    for (const tok of tokens) {
      const variants = TOKEN_VARIANT_MAP.get(tok) || [tok];
      if (!variants.some((v) => titleNorm.includes(v))) {
        return false;
      }
    }

    const exclRaw = String(rule?.exclude_terms || "");
    if (exclRaw) {
      const exclList = exclRaw.split(",").map((x) => normalizeAlertKeyword(x)).filter(Boolean);
      for (const ex of exclList) {
        if (titleNorm.includes(ex)) return false;
      }
    }
  }

  const maxPrice = Number(rule?.max_price || 0);
  const salePrice = Number(linkRow?.sale_price || 0);
  if (maxPrice > 0 && (salePrice <= 0 || salePrice > maxPrice)) {
    return false;
  }

  const minDisc = Number(rule?.min_discount || 0);
  const discRate = Number(linkRow?.discount_rate || 0);
  if (minDisc > 0 && discRate < minDisc) {
    return false;
  }

  return true;
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
  const unitInfo = String(body?.unit_info || "").trim().slice(0, 64);
  const shippingLabel = String(body?.shipping_label || "").trim().slice(0, 32);
  const optionKey = String(body?.option_key || "").trim().slice(0, 120);

  if (dealId) {
    const existing = await env.DB.prepare(
      "SELECT code FROM links WHERE deal_id = ? AND target_url = ? LIMIT 1"
    )
      .bind(dealId, targetUrl)
      .first();
    if (existing && existing.code) {
      if (unitInfo || shippingLabel || optionKey) {
        await env.DB.prepare(
          `UPDATE links
           SET unit_info = CASE WHEN ? != '' THEN ? ELSE COALESCE(unit_info, '') END,
               shipping_label = CASE WHEN ? != '' THEN ? ELSE COALESCE(shipping_label, '') END,
               option_key = CASE WHEN ? != '' THEN ? ELSE COALESCE(option_key, '') END
           WHERE code = ?`
        )
          .bind(unitInfo, unitInfo, shippingLabel, shippingLabel, optionKey, optionKey, existing.code)
          .run()
          .catch(() => {});
      }
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
          title, sale_price, discount_rate, unit_info, shipping_label, option_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(
          candidate,
          targetUrl,
          dealId,
          productId,
          source,
          track,
          title,
          salePrice,
          discountRate,
          unitInfo,
          shippingLabel,
          optionKey,
          createdAt
        )
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

async function queryClickStats(env, origin, days = 7, limit = 10) {
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
    `SELECT l.code, l.deal_id, l.product_id, l.title, l.source, l.sale_price, l.discount_rate, COUNT(*) AS clicks
     FROM clicks c JOIN links l ON c.code = l.code
     WHERE c.kst_date = ?
     GROUP BY l.code, l.deal_id, l.product_id, l.title, l.source, l.sale_price, l.discount_rate
     ORDER BY clicks DESC, MAX(c.id) DESC
     LIMIT ?`
  )
    .bind(todayKst, limit)
    .all();

  const topPeriodRes = await env.DB.prepare(
    `SELECT l.code, l.deal_id, l.product_id, l.title, l.source, l.sale_price, l.discount_rate, COUNT(*) AS clicks
     FROM clicks c JOIN links l ON c.code = l.code
     WHERE c.kst_date >= ?
     GROUP BY l.code, l.deal_id, l.product_id, l.title, l.source, l.sale_price, l.discount_rate
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

  const closedDealIds = await getClosedDealIdsArray(env, 2);
  const attachShortUrl = (rows) =>
    (rows || []).map((r) => ({ ...r, short_url: r.code ? `${origin}/r/${r.code}` : "" }));

  return {
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
    top_today: attachShortUrl(topTodayRes?.results),
    top_period: attachShortUrl(topPeriodRes?.results),
    recent: recentRes?.results || [],
    closed_deal_ids: closedDealIds,
  };
}

async function handleStats(request, env, url) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const days = clampInt(url.searchParams.get("days"), 7, 1, 30);
  const limit = clampInt(url.searchParams.get("limit"), 10, 1, 30);
  const stats = await queryClickStats(env, url.origin, days, limit);
  return jsonResponse(stats);
}

async function readUserPrefs(env, chatId) {
  const row = await env.DB.prepare(
    "SELECT paused, quiet_night FROM alert_prefs WHERE chat_id = ? LIMIT 1"
  )
    .bind(chatId)
    .first();
  return {
    paused: Number(row?.paused ?? 0) === 1 ? 1 : 0,
    quiet_night: row ? (Number(row.quiet_night ?? 1) === 1 ? 1 : 0) : 1,
  };
}

async function findRecentMatches(env, origin, rules, days = 14, limit = 5) {
  if (!Array.isArray(rules) || rules.length === 0) return [];
  const startIso = new Date(Date.now() - days * 86400 * 1000).toISOString();
  const res = await env.DB.prepare(
    `SELECT code, product_id, title, source, sale_price, discount_rate, created_at
     FROM links
     WHERE created_at >= ? AND title != ''
     ORDER BY created_at DESC
     LIMIT 150`
  )
    .bind(startIso)
    .all();
  const rows = res?.results || [];
  const out = [];
  const seenCodes = new Set();
  for (const row of rows) {
    if (!row?.code || seenCodes.has(row.code)) continue;
    const matchedKws = [];
    for (const rule of rules) {
      if (matchLinkAgainstRule(row, rule)) {
        const kn = normalizeKeywordSafe(rule.keyword_norm || rule.keyword);
        const kwLabel = kn.startsWith("pid:") ? `상품가격하락(#${kn.slice(4)})` : String(rule.keyword || "").trim();
        if (kwLabel && !matchedKws.includes(kwLabel)) matchedKws.push(kwLabel);
      }
    }
    if (matchedKws.length > 0) {
      seenCodes.add(row.code);
      out.push({
        code: row.code,
        short_url: `${origin}/r/${row.code}`,
        title: row.title || "",
        source: row.source || "",
        sale_price: Number(row.sale_price || 0),
        discount_rate: Number(row.discount_rate || 0),
        created_at: row.created_at || "",
        matched_keywords: matchedKws,
      });
      if (out.length >= limit) break;
    }
  }
  return out;
}

function normalizeKeywordSafe(raw) {
  return normalizeAlertKeyword(raw);
}

async function listUserAlerts(env, chatId) {
  const res = await env.DB.prepare(
    `SELECT a.id, a.chat_id, a.keyword, a.keyword_norm,
            COALESCE(a.max_price, 0) AS max_price,
            COALESCE(a.min_discount, 0) AS min_discount,
            COALESCE(a.exclude_terms, '') AS exclude_terms,
            COALESCE(a.hit_count, 0) AS hit_count,
            COALESCE(a.last_notified_at, '') AS last_notified_at,
            a.created_at,
            COALESCE(p.paused, 0) AS paused,
            COALESCE(p.quiet_night, 1) AS quiet_night
     FROM keyword_alerts a
     LEFT JOIN alert_prefs p ON p.chat_id = a.chat_id
     WHERE a.chat_id = ?
     ORDER BY a.id ASC
     LIMIT ?`
  )
    .bind(chatId, MAX_KEYWORDS_PER_USER)
    .all();
  const prefs = await readUserPrefs(env, chatId);
  return { alerts: res?.results || [], prefs };
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
    const data = await listUserAlerts(env, chatId);
    return jsonResponse({ ok: true, alerts: data.alerts, prefs: data.prefs });
  }
  const res = await env.DB.prepare(
    `SELECT a.id, a.chat_id, a.keyword, a.keyword_norm,
            COALESCE(a.max_price, 0) AS max_price,
            COALESCE(a.min_discount, 0) AS min_discount,
            COALESCE(a.exclude_terms, '') AS exclude_terms,
            COALESCE(a.hit_count, 0) AS hit_count,
            COALESCE(a.last_notified_at, '') AS last_notified_at,
            a.created_at,
            COALESCE(p.paused, 0) AS paused,
            COALESCE(p.quiet_night, 1) AS quiet_night
     FROM keyword_alerts a
     LEFT JOIN alert_prefs p ON p.chat_id = a.chat_id
     ORDER BY a.id ASC
     LIMIT 2000`
  ).all();
  return jsonResponse({ ok: true, alerts: res?.results || [] });
}

async function createOrUpdateAlert(env, origin, chatId, rawKeyword, rawMaxPrice = 0, rawMinDiscount = 0, rawExcludeTerms = "") {
  const keyword = String(rawKeyword || "").trim().replace(/\s+/g, " ");
  const keywordNorm = normalizeAlertKeyword(keyword);
  if (keyword.length < 2 || keyword.length > 24 || keywordNorm.length < 2) {
    return { ok: false, error: "invalid_keyword" };
  }

  const maxPrice = clampInt(rawMaxPrice, 0, 0, 100000000);
  const minDiscount = clampInt(rawMinDiscount, 0, 0, 95);
  const excludeTerms = sanitizeExcludeTerms(rawExcludeTerms || "");

  const existing = await env.DB.prepare(
    `SELECT id, keyword, COALESCE(max_price, 0) AS max_price,
            COALESCE(min_discount, 0) AS min_discount,
            COALESCE(exclude_terms, '') AS exclude_terms
     FROM keyword_alerts WHERE chat_id = ? AND keyword_norm = ? LIMIT 1`
  )
    .bind(chatId, keywordNorm)
    .first();

  if (existing && existing.keyword) {
    const changed =
      Number(existing.max_price || 0) !== maxPrice ||
      Number(existing.min_discount || 0) !== minDiscount ||
      String(existing.exclude_terms || "") !== excludeTerms ||
      String(existing.keyword || "") !== keyword;
    if (changed) {
      await env.DB.prepare(
        `UPDATE keyword_alerts
         SET keyword = ?, max_price = ?, min_discount = ?, exclude_terms = ?
         WHERE chat_id = ? AND keyword_norm = ?`
      )
        .bind(keyword, maxPrice, minDiscount, excludeTerms, chatId, keywordNorm)
        .run();
    }
    const cntRow = await env.DB.prepare(
      "SELECT COUNT(*) AS cnt FROM keyword_alerts WHERE chat_id = ?"
    )
      .bind(chatId)
      .first();
    const ruleObj = {
      keyword,
      keyword_norm: keywordNorm,
      max_price: maxPrice,
      min_discount: minDiscount,
      exclude_terms: excludeTerms,
      paused: 0,
    };
    const recentMatches = changed ? await findRecentMatches(env, origin, [ruleObj], 14, 3) : [];
    return {
      ok: true,
      id: Number(existing.id || 0),
      keyword: changed ? keyword : existing.keyword,
      keyword_norm: keywordNorm,
      max_price: maxPrice,
      min_discount: minDiscount,
      exclude_terms: excludeTerms,
      count: Number(cntRow?.cnt || 1),
      already_exists: true,
      updated: changed,
      recent_matches: recentMatches,
    };
  }

  const cntRow = await env.DB.prepare(
    "SELECT COUNT(*) AS cnt FROM keyword_alerts WHERE chat_id = ?"
  )
    .bind(chatId)
    .first();
  const currentCnt = Number(cntRow?.cnt || 0);
  if (currentCnt >= MAX_KEYWORDS_PER_USER) {
    return { ok: false, error: "limit_exceeded", max_keywords: MAX_KEYWORDS_PER_USER };
  }

  const createdAt = new Date().toISOString();
  const insRes = await env.DB.prepare(
    `INSERT OR IGNORE INTO keyword_alerts
      (chat_id, keyword, keyword_norm, max_price, min_discount, exclude_terms, hit_count, last_notified_at, created_at)
     VALUES (?, ?, ?, ?, ?, ?, 0, '', ?)`
  )
    .bind(chatId, keyword, keywordNorm, maxPrice, minDiscount, excludeTerms, createdAt)
    .run();

  const recentMatches = await findRecentMatches(
    env,
    origin,
    [
      {
        keyword,
        keyword_norm: keywordNorm,
        max_price: maxPrice,
        min_discount: minDiscount,
        exclude_terms: excludeTerms,
        paused: 0,
      },
    ],
    14,
    3
  );

  return {
    ok: true,
    id: Number(insRes?.meta?.last_row_id || 0),
    keyword,
    keyword_norm: keywordNorm,
    max_price: maxPrice,
    min_discount: minDiscount,
    exclude_terms: excludeTerms,
    count: currentCnt + 1,
    already_exists: false,
    updated: false,
    recent_matches: recentMatches,
  };
}

async function handleCreateAlert(request, env, origin) {
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
  const result = await createOrUpdateAlert(
    env,
    origin,
    chatId,
    body?.keyword,
    body?.max_price,
    body?.min_discount,
    body?.exclude_terms
  );
  if (!result.ok) {
    return jsonResponse(result, 400);
  }
  return jsonResponse(result);
}

async function deleteUserAlert(env, chatId, { alertId = 0, keyword = "", removeAll = false, purgePrefs = false } = {}) {
  if (removeAll) {
    const info = await env.DB.prepare("DELETE FROM keyword_alerts WHERE chat_id = ?")
      .bind(chatId)
      .run();
    if (purgePrefs) {
      await env.DB.prepare("DELETE FROM alert_prefs WHERE chat_id = ?").bind(chatId).run();
    }
    return { ok: true, deleted: Number(info?.meta?.changes ?? 0) };
  }
  if (alertId > 0) {
    const info = await env.DB.prepare("DELETE FROM keyword_alerts WHERE chat_id = ? AND id = ?")
      .bind(chatId, alertId)
      .run();
    return { ok: true, deleted: Number(info?.meta?.changes ?? 0) };
  }
  const keywordNorm = normalizeAlertKeyword(keyword || "");
  if (!keywordNorm) {
    return { ok: false, error: "invalid_keyword" };
  }
  const info = await env.DB.prepare("DELETE FROM keyword_alerts WHERE chat_id = ? AND keyword_norm = ?")
    .bind(chatId, keywordNorm)
    .run();
  return { ok: true, deleted: Number(info?.meta?.changes ?? 0) };
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
    const purgePrefs = url.searchParams.get("purge_prefs") === "1";
    return jsonResponse(await deleteUserAlert(env, chatId, { removeAll: true, purgePrefs }));
  }
  const idParam = url.searchParams.get("id");
  if (idParam !== null && String(idParam).trim() !== "") {
    const alertId = clampInt(idParam, 0, 1, 2147483647);
    if (!alertId) {
      return jsonResponse({ ok: false, error: "invalid_id" }, 400);
    }
    return jsonResponse(await deleteUserAlert(env, chatId, { alertId }));
  }
  const res = await deleteUserAlert(env, chatId, { keyword: url.searchParams.get("keyword") || "" });
  if (!res.ok) return jsonResponse(res, 400);
  return jsonResponse(res);
}

async function updateUserAlertPrefs(env, chatId, { paused = undefined, quiet_night = undefined } = {}) {
  const cur = await readUserPrefs(env, chatId);
  const nextPaused = paused === undefined ? cur.paused : Number(Boolean(paused)) ? 1 : 0;
  const nextQuiet = quiet_night === undefined ? cur.quiet_night : Number(Boolean(quiet_night)) ? 1 : 0;
  const updatedAt = new Date().toISOString();

  await env.DB.prepare(
    `INSERT INTO alert_prefs (chat_id, paused, quiet_night, updated_at)
     VALUES (?, ?, ?, ?)
     ON CONFLICT(chat_id) DO UPDATE SET
       paused = excluded.paused,
       quiet_night = excluded.quiet_night,
       updated_at = excluded.updated_at`
  )
    .bind(chatId, nextPaused, nextQuiet, updatedAt)
    .run();

  return { paused: nextPaused, quiet_night: nextQuiet };
}

async function handleAlertPrefs(request, env) {
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
  const prefs = await updateUserAlertPrefs(env, chatId, {
    paused: body?.paused,
    quiet_night: body?.quiet_night,
  });
  return jsonResponse({ ok: true, prefs });
}

async function handleAlertHits(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const hits = Array.isArray(body?.hits) ? body.hits.slice(0, 100) : [];
  if (hits.length === 0) {
    return jsonResponse({ ok: true, updated: 0 });
  }
  const nowIso = new Date().toISOString();
  const stmts = [];
  for (const h of hits) {
    const cid = String(h?.chat_id ?? "").trim();
    const kn = normalizeAlertKeyword(h?.keyword_norm || h?.keyword || "");
    if (!CHAT_ID_RE.test(cid) || !kn) continue;
    stmts.push(
      env.DB.prepare(
        `UPDATE keyword_alerts
         SET hit_count = COALESCE(hit_count, 0) + 1, last_notified_at = ?
         WHERE chat_id = ? AND keyword_norm = ?`
      ).bind(nowIso, cid, kn)
    );
  }
  if (stmts.length > 0) {
    await env.DB.batch(stmts);
  }
  return jsonResponse({ ok: true, updated: stmts.length });
}

async function findUserRecentMatches(env, origin, chatId, days = 14, limit = 5) {
  const res = await env.DB.prepare(
    `SELECT keyword, keyword_norm,
            COALESCE(max_price, 0) AS max_price,
            COALESCE(min_discount, 0) AS min_discount,
            COALESCE(exclude_terms, '') AS exclude_terms
     FROM keyword_alerts WHERE chat_id = ? ORDER BY id ASC LIMIT ?`
  )
    .bind(chatId, MAX_KEYWORDS_PER_USER)
    .all();
  const rules = (res?.results || []).map((r) => ({ ...r, paused: 0 }));
  return findRecentMatches(env, origin, rules, days, limit);
}

async function handleRecentAlertMatches(request, env, url) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const days = clampInt(url.searchParams.get("days"), 14, 1, 30);
  const limit = clampInt(url.searchParams.get("limit"), 5, 1, 10);
  const chatId = String(url.searchParams.get("chat_id") || "").trim();

  if (chatId) {
    if (!CHAT_ID_RE.test(chatId)) {
      return jsonResponse({ ok: false, error: "invalid_chat_id" }, 400);
    }
    const matches = await findUserRecentMatches(env, url.origin, chatId, days, limit);
    return jsonResponse({ ok: true, matches });
  }

  const kw = String(url.searchParams.get("keyword") || "").trim().replace(/\s+/g, " ");
  const kn = normalizeAlertKeyword(kw);
  if (!kn) {
    return jsonResponse({ ok: false, error: "invalid_keyword" }, 400);
  }
  const rules = [
    {
      keyword: kw,
      keyword_norm: kn,
      max_price: clampInt(url.searchParams.get("max_price"), 0, 0, 100000000),
      min_discount: clampInt(url.searchParams.get("min_discount"), 0, 0, 95),
      exclude_terms: sanitizeExcludeTerms(url.searchParams.get("exclude_terms") || ""),
      paused: 0,
    },
  ];
  const matches = await findRecentMatches(env, url.origin, rules, days, limit);
  return jsonResponse({ ok: true, matches });
}

async function queryAlertStats(env) {
  const summary = await env.DB.prepare(
    `SELECT COUNT(*) AS total_keywords,
            COUNT(DISTINCT chat_id) AS total_users,
            COALESCE(SUM(hit_count), 0) AS total_hits
     FROM keyword_alerts`
  ).first();
  const pausedRow = await env.DB.prepare(
    `SELECT COUNT(DISTINCT a.chat_id) AS paused_users
     FROM keyword_alerts a
     JOIN alert_prefs p ON p.chat_id = a.chat_id
     WHERE COALESCE(p.paused, 0) = 1`
  ).first();
  const topRes = await env.DB.prepare(
    `SELECT MIN(keyword) AS keyword, keyword_norm,
            COUNT(DISTINCT chat_id) AS subscribers,
            COALESCE(SUM(hit_count), 0) AS hits
     FROM keyword_alerts
     GROUP BY keyword_norm
     ORDER BY subscribers DESC, hits DESC, MIN(id) ASC
     LIMIT 15`
  ).all();

  const watchMetrics = await readWatchMetrics(env);

  return {
    ok: true,
    total_users: Number(summary?.total_users || 0),
    total_keywords: Number(summary?.total_keywords || 0),
    paused_users: Number(pausedRow?.paused_users || 0),
    total_hits: Number(summary?.total_hits || 0),
    top_keywords: topRes?.results || [],
    watch_metrics: watchMetrics,
  };
}

async function handleAlertStats(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  return jsonResponse(await queryAlertStats(env));
}

async function getClosedDealIdsArray(env, days = 3) {
  const closed = new Set();
  try {
    const startIso = new Date(Date.now() - days * 86400 * 1000).toISOString();
    const res = await env.DB.prepare(
      "SELECT items_json, all_closed FROM posted_messages WHERE posted_at >= ? ORDER BY posted_at DESC LIMIT 80"
    )
      .bind(startIso)
      .all();
    for (const row of res?.results || []) {
      let items = [];
      try {
        items = JSON.parse(String(row.items_json || "[]"));
      } catch {
        items = [];
      }
      for (const it of items) {
        if ((it?.closed || Number(row.all_closed || 0) === 1) && it?.deal_id) {
          closed.add(String(it.deal_id));
        }
      }
    }
  } catch {
    // Fallback gracefully if posted_messages does not exist yet
  }
  return Array.from(closed);
}

async function recordChannelMemberCount(env, count) {
  const now = new Date();
  const nowIso = now.toISOString();
  const { kstDate } = toKstParts(now);

  const metaRow = await env.DB.prepare(
    "SELECT value FROM channel_meta WHERE key = 'member_count' LIMIT 1"
  ).first();
  const initialized = !metaRow;
  const prevCount = metaRow ? Number.parseInt(String(metaRow.value || "0"), 10) : null;
  const delta = prevCount === null || Number.isNaN(prevCount) ? 0 : count - prevCount;

  await env.DB.prepare(
    `INSERT INTO channel_meta (key, value, updated_at)
     VALUES ('member_count', ?, ?)
     ON CONFLICT(key) DO UPDATE SET
       value = excluded.value,
       updated_at = excluded.updated_at`
  )
    .bind(String(count), nowIso)
    .run();

  const todayRow = await env.DB.prepare(
    "SELECT start_count, latest_count, peak_count FROM channel_stats WHERE kst_date = ? LIMIT 1"
  )
    .bind(kstDate)
    .first();

  let todayStart = count;
  if (!todayRow) {
    todayStart = prevCount !== null && !Number.isNaN(prevCount) ? prevCount : count;
    await env.DB.prepare(
      `INSERT INTO channel_stats (kst_date, start_count, latest_count, peak_count, updated_at)
       VALUES (?, ?, ?, ?, ?)`
    )
      .bind(kstDate, todayStart, count, Math.max(todayStart, count), nowIso)
      .run();
  } else {
    todayStart = Number(todayRow.start_count ?? count);
    const peakCount = Math.max(Number(todayRow.peak_count ?? count), count);
    await env.DB.prepare(
      `UPDATE channel_stats
       SET latest_count = ?, peak_count = ?, updated_at = ?
       WHERE kst_date = ?`
    )
      .bind(count, peakCount, nowIso, kstDate)
      .run();
  }

  let milestone = 0;
  if (prevCount !== null && !Number.isNaN(prevCount) && delta > 0) {
    for (const m of MEMBER_MILESTONES) {
      if (prevCount < m && count >= m) {
        milestone = m;
      }
    }
  }

  return {
    ok: true,
    prev_count: prevCount === null ? count : prevCount,
    count,
    delta,
    today_start: todayStart,
    today_net: count - todayStart,
    milestone,
    initialized,
  };
}

async function handleChannelMembersPost(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const rawCount = Number.parseInt(String(body?.count ?? ""), 10);
  if (Number.isNaN(rawCount) || rawCount < 0) {
    return jsonResponse({ ok: false, error: "invalid_count" }, 400);
  }
  return jsonResponse(await recordChannelMemberCount(env, rawCount));
}

async function queryChannelMemberStats(env) {
  const { kstDate } = toKstParts(new Date());
  const metaRow = await env.DB.prepare(
    "SELECT value FROM channel_meta WHERE key = 'member_count' LIMIT 1"
  ).first();
  const curCount = metaRow ? Number.parseInt(String(metaRow.value || "0"), 10) : 0;

  const todayRow = await env.DB.prepare(
    "SELECT start_count, latest_count, peak_count FROM channel_stats WHERE kst_date = ? LIMIT 1"
  )
    .bind(kstDate)
    .first();
  const todayStart = todayRow ? Number(todayRow.start_count ?? curCount) : curCount;

  const histRes = await env.DB.prepare(
    "SELECT kst_date, start_count, latest_count, peak_count FROM channel_stats ORDER BY kst_date DESC LIMIT 7"
  ).all();

  return {
    ok: true,
    count: curCount,
    today_start: todayStart,
    today_net: curCount - todayStart,
    history: histRes?.results || [],
  };
}

async function handleChannelMembersGet(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  return jsonResponse(await queryChannelMemberStats(env));
}

async function upsertFlyerStatus(env, payload) {
  const data = {
    date: String(payload?.date || "").trim().slice(0, 16),
    community_sent: clampInt(payload?.community_sent, 0, 0, 100000),
    goldbox_sent: clampInt(payload?.goldbox_sent, 0, 0, 100000),
    waiting: clampInt(payload?.waiting, 0, 0, 10000),
    updated_at: new Date().toISOString(),
  };
  await env.DB.prepare(
    `INSERT INTO channel_meta (key, value, updated_at)
     VALUES ('flyer_status', ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
  )
    .bind(JSON.stringify(data), data.updated_at)
    .run();
  return data;
}

async function readFlyerStatus(env) {
  try {
    const row = await env.DB.prepare("SELECT value FROM channel_meta WHERE key = 'flyer_status' LIMIT 1").first();
    if (!row?.value) return null;
    return JSON.parse(String(row.value));
  } catch {
    return null;
  }
}

async function handleChannelStatusPost(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const status = await upsertFlyerStatus(env, body);
  return jsonResponse({ ok: true, status });
}

async function handleChannelStatusGet(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const status = await readFlyerStatus(env);
  return jsonResponse({ ok: true, status });
}

async function handleCreatePost(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const messageId = clampInt(body?.message_id, 0, 1, 2147483647000);
  const chatId = String(body?.chat_id || "").trim();
  const caption = String(body?.caption || "");
  const items = Array.isArray(body?.items) ? body.items : [];
  const inlineKb = Array.isArray(body?.inline_kb) ? body.inline_kb : [];
  if (!messageId || !chatId) {
    return jsonResponse({ ok: false, error: "invalid_post" }, 400);
  }
  const nowIso = new Date().toISOString();
  await env.DB.prepare(
    `INSERT OR REPLACE INTO posted_messages
      (message_id, chat_id, caption, items_json, inline_kb_json, all_closed, posted_at, last_checked_at)
     VALUES (?, ?, ?, ?, ?, 0, ?, '')`
  )
    .bind(messageId, chatId, caption, JSON.stringify(items), JSON.stringify(inlineKb), nowIso)
    .run();

  return jsonResponse({ ok: true, message_id: messageId });
}

async function handleGetActivePosts(request, env, url) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const hours = clampInt(url.searchParams.get("hours"), 6, 1, 24);
  const minRecheckMin = clampInt(url.searchParams.get("min_recheck_min"), 20, 0, 120);
  const limit = clampInt(url.searchParams.get("limit"), 4, 1, 10);

  const nowMs = Date.now();
  const cutoffIso = new Date(nowMs - hours * 3600 * 1000).toISOString();
  const recheckCutoffIso = new Date(nowMs - minRecheckMin * 60 * 1000).toISOString();

  const res = await env.DB.prepare(
    `SELECT message_id, chat_id, caption, items_json, inline_kb_json, all_closed, posted_at, last_checked_at
     FROM posted_messages
     WHERE COALESCE(all_closed, 0) = 0
       AND posted_at >= ?
       AND (last_checked_at = '' OR last_checked_at <= ?)
     ORDER BY posted_at DESC
     LIMIT ?`
  )
    .bind(cutoffIso, recheckCutoffIso, limit)
    .all();

  const posts = (res?.results || []).map((r) => {
    let items = [];
    let inlineKb = [];
    try {
      items = JSON.parse(String(r.items_json || "[]"));
    } catch {
      items = [];
    }
    try {
      inlineKb = JSON.parse(String(r.inline_kb_json || "[]"));
    } catch {
      inlineKb = [];
    }
    return {
      message_id: Number(r.message_id),
      chat_id: r.chat_id,
      caption: r.caption,
      items,
      inline_kb: inlineKb,
      all_closed: Number(r.all_closed || 0),
      posted_at: r.posted_at,
      last_checked_at: r.last_checked_at || "",
    };
  });

  return jsonResponse({ ok: true, posts });
}

async function handleUpdatePost(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const messageId = clampInt(body?.message_id, 0, 1, 2147483647000);
  if (!messageId) {
    return jsonResponse({ ok: false, error: "invalid_message_id" }, 400);
  }
  const existing = await env.DB.prepare(
    "SELECT caption, items_json, inline_kb_json, all_closed FROM posted_messages WHERE message_id = ? LIMIT 1"
  )
    .bind(messageId)
    .first();
  if (!existing) {
    return jsonResponse({ ok: false, error: "not_found" }, 404);
  }

  const nextCaption = body?.caption !== undefined ? String(body.caption) : existing.caption;
  const nextItemsJson =
    body?.items !== undefined ? JSON.stringify(Array.isArray(body.items) ? body.items : []) : existing.items_json;
  const nextKbJson =
    body?.inline_kb !== undefined
      ? JSON.stringify(Array.isArray(body.inline_kb) ? body.inline_kb : [])
      : existing.inline_kb_json;
  const nextAllClosed =
    body?.all_closed !== undefined ? (body.all_closed ? 1 : 0) : Number(existing.all_closed || 0);
  const nowIso = new Date().toISOString();

  await env.DB.prepare(
    `UPDATE posted_messages
     SET caption = ?, items_json = ?, inline_kb_json = ?, all_closed = ?, last_checked_at = ?
     WHERE message_id = ?`
  )
    .bind(nextCaption, nextItemsJson, nextKbJson, nextAllClosed, nowIso, messageId)
    .run();

  return jsonResponse({ ok: true, message_id: messageId, all_closed: nextAllClosed });
}

async function handlePublicFeed(env, url) {
  const limit = clampInt(url.searchParams.get("limit"), 6, 1, 12);
  const days = clampInt(url.searchParams.get("days"), 3, 1, 7);
  const startIso = new Date(Date.now() - days * 86400 * 1000).toISOString();

  const [linksRes, closedDealIds, memberRow] = await Promise.all([
    env.DB.prepare(
      `SELECT l.code, l.deal_id, l.product_id, l.source, l.title, l.sale_price, l.discount_rate, l.created_at,
              COUNT(c.id) AS clicks
       FROM links l
       LEFT JOIN clicks c ON c.code = l.code
       WHERE l.created_at >= ?
         AND l.title != ''
         AND COALESCE(l.source, '') != 'goldbox'
       GROUP BY l.code, l.deal_id, l.product_id, l.source, l.title, l.sale_price, l.discount_rate, l.created_at
       ORDER BY l.created_at DESC
       LIMIT 60`
    )
      .bind(startIso)
      .all(),
    getClosedDealIdsArray(env, days),
    env.DB.prepare("SELECT value FROM channel_meta WHERE key = 'member_count' LIMIT 1")
      .first()
      .catch(() => null),
  ]);

  const closedSet = new Set(closedDealIds);
  const seenKeys = new Set();
  const items = [];
  for (const r of linksRes?.results || []) {
    const did = String(r.deal_id || "").trim();
    if (did && closedSet.has(did)) continue;
    const dedupKey = String(r.product_id || r.code || "").trim();
    if (!dedupKey || seenKeys.has(dedupKey)) continue;
    seenKeys.add(dedupKey);
    const src = String(r.source || "").trim().toLowerCase();
    items.push({
      code: r.code,
      short_url: `${url.origin}/r/${r.code}`,
      title: r.title || "",
      source: src,
      source_kr: SOURCE_KR_MAP[src] || src || "커뮤니티",
      sale_price: Number(r.sale_price || 0),
      discount_rate: Number(r.discount_rate || 0),
      clicks: Number(r.clicks || 0),
      created_at: r.created_at || "",
    });
    if (items.length >= limit) break;
  }

  return publicJsonResponse({
    ok: true,
    updated_at: new Date().toISOString(),
    channel_url: "https://t.me/HOTpang911",
    member_count: memberRow ? Number.parseInt(String(memberRow.value || "0"), 10) || 0 : 0,
    items,
  });
}

const dbOps = {
  queryClickStats,
  queryAlertStats,
  recordChannelMemberCount,
  queryChannelMemberStats,
  listUserAlerts,
  createOrUpdateAlert,
  deleteUserAlert,
  updateUserAlertPrefs,
  findUserRecentMatches,
  readFlyerStatus,
  getLinksByCodes,
  bumpWatchMetric,
  readWatchMetrics,
  readSourceFunnelSummary,
};

async function handleTelegramWebhook(request, env, ctx, origin) {
  const expectedSecret = String(env?.TELEGRAM_WEBHOOK_SECRET || "").trim();
  if (!expectedSecret) {
    return jsonResponse({ ok: false, error: "webhook_not_configured" }, 401);
  }
  const providedSecret = String(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "").trim();
  if (!constantTimeEqual(providedSecret, expectedSecret)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }

  let update;
  try {
    update = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }

  const work = handleTelegramUpdate(update, env, origin, dbOps).catch((err) => {
    console.error(JSON.stringify({ event: "telegram_webhook_error", message: String(err) }));
  });
  if (ctx && typeof ctx.waitUntil === "function") {
    ctx.waitUntil(work);
    return jsonResponse({ ok: true });
  }
  await work;
  return jsonResponse({ ok: true });
}

async function readChannelMeta(env, key) {
  try {
    const row = await env.DB.prepare("SELECT value FROM channel_meta WHERE key = ? LIMIT 1").bind(key).first();
    return row ? String(row.value || "") : null;
  } catch {
    return null;
  }
}

async function writeChannelMeta(env, key, value, nowIso = new Date().toISOString()) {
  try {
    await env.DB.prepare(
      `INSERT INTO channel_meta (key, value, updated_at)
       VALUES (?, ?, ?)
       ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
    )
      .bind(key, String(value), nowIso)
      .run();
  } catch (err) {
    console.warn("writeChannelMeta failed", key, String(err));
  }
}

const LINK_CODE_RE = /^[A-Za-z0-9]{4,32}$/;

async function getLinksByCodes(env, codes = []) {
  const cleanCodes = [];
  const seen = new Set();
  for (const raw of Array.isArray(codes) ? codes : []) {
    const c = String(raw || "").trim();
    if (!LINK_CODE_RE.test(c) || seen.has(c)) continue;
    seen.add(c);
    cleanCodes.push(c);
    if (cleanCodes.length >= 6) break;
  }
  if (cleanCodes.length === 0) return [];
  const placeholders = cleanCodes.map(() => "?").join(", ");
  const res = await env.DB.prepare(
    `SELECT code, target_url, deal_id, product_id, source, track, title,
            sale_price, discount_rate,
            COALESCE(unit_info, '') AS unit_info,
            COALESCE(shipping_label, '') AS shipping_label,
            COALESCE(option_key, '') AS option_key,
            created_at
     FROM links
     WHERE code IN (${placeholders})`
  )
    .bind(...cleanCodes)
    .all();
  const byCode = new Map();
  for (const row of res?.results || []) {
    byCode.set(String(row.code), row);
  }
  return cleanCodes.map((c) => byCode.get(c)).filter(Boolean);
}

async function readWatchMetrics(env) {
  try {
    const raw = await readChannelMeta(env, "watch_deep_link_metrics");
    if (!raw) return { opened: 0, registered: 0, updated_at: "" };
    const parsed = JSON.parse(raw);
    return {
      opened: Math.max(0, Number.parseInt(String(parsed?.opened ?? 0), 10) || 0),
      registered: Math.max(0, Number.parseInt(String(parsed?.registered ?? 0), 10) || 0),
      updated_at: String(parsed?.updated_at || ""),
    };
  } catch {
    return { opened: 0, registered: 0, updated_at: "" };
  }
}

async function bumpWatchMetric(env, field) {
  if (field !== "opened" && field !== "registered") return null;
  const cur = await readWatchMetrics(env);
  const nowIso = new Date().toISOString();
  const next = {
    opened: Number(cur.opened || 0) + (field === "opened" ? 1 : 0),
    registered: Number(cur.registered || 0) + (field === "registered" ? 1 : 0),
    updated_at: nowIso,
  };
  await writeChannelMeta(env, "watch_deep_link_metrics", JSON.stringify(next), nowIso);
  return next;
}

function sanitizeSalesState(raw) {
  const obj = raw && typeof raw === "object" ? raw : {};
  const hashesIn = Array.isArray(obj.notified_order_hashes) ? obj.notified_order_hashes : [];
  const hashSet = new Set();
  const hashes = [];
  for (const h of hashesIn) {
    const s = String(h || "").trim();
    if (!/^[a-f0-9]{16,128}$/i.test(s) || hashSet.has(s)) continue;
    hashSet.add(s);
    hashes.push(s.toLowerCase());
  }
  const cappedHashes = hashes.length > 500 ? hashes.slice(-500) : hashes;
  const dailySummary = {};
  if (obj.daily_summary_sent && typeof obj.daily_summary_sent === "object") {
    for (const [k, v] of Object.entries(obj.daily_summary_sent)) {
      if (/^\d{8}$/.test(String(k))) dailySummary[String(k)] = Boolean(v);
    }
  }
  const monthMilestone = {};
  if (obj.month_milestone_sent && typeof obj.month_milestone_sent === "object") {
    for (const [k, v] of Object.entries(obj.month_milestone_sent)) {
      if (/^\d{6}$/.test(String(k))) {
        monthMilestone[String(k)] = Math.max(0, Number.parseInt(String(v ?? 0), 10) || 0);
      }
    }
  }
  return {
    notified_order_hashes: cappedHashes,
    month: /^\d{6}$/.test(String(obj.month || "")) ? String(obj.month) : "",
    month_milestone_step: Math.max(0, Number.parseInt(String(obj.month_milestone_step ?? 0), 10) || 0),
    milestone_retry_pending: Boolean(obj.milestone_retry_pending),
    daily_summary_sent: dailySummary,
    month_milestone_sent: monthMilestone,
    updated_at: String(obj.updated_at || new Date().toISOString()),
  };
}

async function handleSalesStateGet(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const raw = await readChannelMeta(env, "sales_tracker_state");
  let state = sanitizeSalesState({});
  if (raw) {
    try {
      state = sanitizeSalesState(JSON.parse(raw));
    } catch {
      state = sanitizeSalesState({});
    }
  }
  return jsonResponse({ ok: true, state });
}

async function handleSalesStatePost(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const next = sanitizeSalesState({ ...(body || {}), updated_at: new Date().toISOString() });
  await writeChannelMeta(env, "sales_tracker_state", JSON.stringify(next), next.updated_at);
  return jsonResponse({ ok: true, state: next });
}

function mergeSourceFunnelEntry(existingSources, incomingSources) {
  const out = {};
  const srcObj = existingSources && typeof existingSources === "object" ? existingSources : {};
  for (const [src, vals] of Object.entries(srcObj)) {
    if (!vals || typeof vals !== "object") continue;
    out[src] = {
      found: Number(vals.found || 0),
      new: Number(vals.new || 0),
      price_ok: Number(vals.price_ok || 0),
      passed: Number(vals.passed || 0),
      blocked: Number(vals.blocked || 0),
      reasons: vals.reasons && typeof vals.reasons === "object" ? { ...vals.reasons } : {},
    };
  }
  const incObj = incomingSources && typeof incomingSources === "object" ? incomingSources : {};
  for (const [rawSrc, rawVals] of Object.entries(incObj)) {
    const src = String(rawSrc || "").trim().toLowerCase().slice(0, 32);
    if (!src || !rawVals || typeof rawVals !== "object") continue;
    if (!out[src]) {
      out[src] = { found: 0, new: 0, price_ok: 0, passed: 0, blocked: 0, reasons: {} };
    }
    out[src].found += Math.max(0, Number.parseInt(String(rawVals.found ?? 0), 10) || 0);
    out[src].new += Math.max(0, Number.parseInt(String(rawVals.new ?? 0), 10) || 0);
    out[src].price_ok += Math.max(0, Number.parseInt(String(rawVals.price_ok ?? 0), 10) || 0);
    out[src].passed += Math.max(0, Number.parseInt(String(rawVals.passed ?? 0), 10) || 0);
    out[src].blocked += Math.max(0, Number.parseInt(String(rawVals.blocked ?? 0), 10) || 0);
    const incReasons = rawVals.reasons && typeof rawVals.reasons === "object" ? rawVals.reasons : {};
    for (const [rk, rv] of Object.entries(incReasons)) {
      const reasonKey = String(rk || "").trim().slice(0, 48);
      if (!reasonKey) continue;
      out[src].reasons[reasonKey] = Number(out[src].reasons[reasonKey] || 0) + Math.max(0, Number.parseInt(String(rv ?? 0), 10) || 0);
    }
  }
  return out;
}

async function handleChannelFunnelPost(request, env) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid_json" }, 400);
  }
  const { kstDate } = toKstParts(new Date());
  const dateKey = /^\d{4}-\d{2}-\d{2}$/.test(String(body?.date || "")) ? String(body.date) : kstDate;
  const metaKey = `source_funnel_${dateKey}`;
  let cur = { date: dateKey, runs: 0, sources: {}, updated_at: "" };
  const raw = await readChannelMeta(env, metaKey);
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        cur = {
          date: dateKey,
          runs: Number(parsed.runs || 0),
          sources: parsed.sources || {},
          updated_at: String(parsed.updated_at || ""),
        };
      }
    } catch {}
  }
  const addRuns = Math.max(1, Number.parseInt(String(body?.runs ?? 1), 10) || 1);
  const nowIso = new Date().toISOString();
  const next = {
    date: dateKey,
    runs: cur.runs + addRuns,
    sources: mergeSourceFunnelEntry(cur.sources, body?.sources),
    updated_at: nowIso,
  };
  await writeChannelMeta(env, metaKey, JSON.stringify(next), nowIso);
  return jsonResponse({ ok: true, funnel: next });
}

async function readSourceFunnelSummary(env, days = 1) {
  const safeDays = clampInt(days, 1, 1, 7);
  const nowMs = Date.now();
  const keys = [];
  for (let i = 0; i < safeDays; i += 1) {
    const { kstDate } = toKstParts(new Date(nowMs - i * 86400 * 1000));
    keys.push(`source_funnel_${kstDate}`);
  }
  let totalRuns = 0;
  let mergedSources = {};
  let latestUpdated = "";
  for (const key of keys) {
    const raw = await readChannelMeta(env, key);
    if (!raw) continue;
    try {
      const parsed = JSON.parse(raw);
      totalRuns += Number(parsed?.runs || 0);
      mergedSources = mergeSourceFunnelEntry(mergedSources, parsed?.sources);
      if (parsed?.updated_at && String(parsed.updated_at) > latestUpdated) {
        latestUpdated = String(parsed.updated_at);
      }
    } catch {}
  }
  return {
    days: safeDays,
    date: toKstParts(new Date(nowMs)).kstDate,
    runs: totalRuns,
    sources: mergedSources,
    updated_at: latestUpdated,
  };
}

async function handleChannelFunnelGet(request, env, url) {
  if (!isAuthorized(request, env)) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  const days = clampInt(url.searchParams.get("days"), 1, 1, 7);
  const funnel = await readSourceFunnelSummary(env, days);
  return jsonResponse({ ok: true, ...funnel });
}

async function sendWatchdogAlert(env, message, nowMs = Date.now(), throttleMs = 6 * 3600 * 1000) {
  const adminId = parseAdminChatId(env);
  if (adminId === null || !String(env?.TELEGRAM_BOT_TOKEN || "").trim()) return false;
  const lastIso = await readChannelMeta(env, "last_watchdog_alert");
  if (lastIso) {
    const lastMs = Date.parse(lastIso);
    if (!Number.isNaN(lastMs) && nowMs - lastMs < throttleMs) {
      return false;
    }
  }
  await writeChannelMeta(env, "last_watchdog_alert", new Date(nowMs).toISOString(), new Date(nowMs).toISOString());
  try {
    await tgApi(env, "sendMessage", {
      chat_id: adminId,
      text: message,
      parse_mode: "HTML",
    });
    return true;
  } catch (err) {
    console.warn("sendWatchdogAlert failed", String(err));
    return false;
  }
}

export async function runDailyMaintenance(env, nowDate = new Date()) {
  const nowMs = nowDate.getTime();
  const cutoff30d = new Date(nowMs - 30 * 86400 * 1000).toISOString();
  const cutoff14d = new Date(nowMs - 14 * 86400 * 1000).toISOString();
  const cutoff365d = new Date(nowMs - 365 * 86400 * 1000).toISOString();

  const pruned = { sessions: 0, posts: 0, clicks: 0, links: 0, funnels: 0 };
  const funnelCutoffKey = `source_funnel_${toKstParts(new Date(nowMs - 14 * 86400 * 1000)).kstDate}`;
  try {
    const rSess = await env.DB.prepare("DELETE FROM bot_sessions WHERE updated_at < ?").bind(cutoff30d).run();
    const rPost = await env.DB.prepare("DELETE FROM posted_messages WHERE posted_at < ?").bind(cutoff14d).run();
    const rClicks = await env.DB.prepare("DELETE FROM clicks WHERE clicked_at < ?").bind(cutoff365d).run();
    const rLinks = await env.DB.prepare(
      "DELETE FROM links WHERE created_at < ? AND code NOT IN (SELECT DISTINCT code FROM clicks)"
    )
      .bind(cutoff365d)
      .run();
    const rFunnels = await env.DB.prepare(
      "DELETE FROM channel_meta WHERE key LIKE 'source_funnel_%' AND key < ?"
    )
      .bind(funnelCutoffKey)
      .run()
      .catch(() => ({ meta: { changes: 0 } }));
    pruned.sessions = Number(rSess?.meta?.changes || 0);
    pruned.posts = Number(rPost?.meta?.changes || 0);
    pruned.clicks = Number(rClicks?.meta?.changes || 0);
    pruned.links = Number(rLinks?.meta?.changes || 0);
    pruned.funnels = Number(rFunnels?.meta?.changes || 0);
  } catch (err) {
    console.warn(JSON.stringify({ event: "d1_prune_error", message: String(err) }));
  }

  let webhookHealed = false;
  const botToken = String(env?.TELEGRAM_BOT_TOKEN || "").trim();
  const whSecret = String(env?.TELEGRAM_WEBHOOK_SECRET || "").trim();
  if (botToken && whSecret) {
    try {
      const baseUrl = String(env?.WORKER_BASE_URL || "https://hotpang-click.produckyou.workers.dev").replace(/\/+$/, "");
      const expectedUrl = `${baseUrl}/telegram/webhook`;
      const info = await tgApi(env, "getWebhookInfo", {});
      const allowed = Array.isArray(info?.allowed_updates) ? info.allowed_updates : [];
      if (info?.url !== expectedUrl || !allowed.includes("chat_member") || !allowed.includes("message")) {
        await tgApi(env, "setWebhook", {
          url: expectedUrl,
          secret_token: whSecret,
          allowed_updates: ["message", "callback_query", "chat_member"],
          drop_pending_updates: false,
        });
        webhookHealed = true;
        console.warn(JSON.stringify({ event: "webhook_self_healed", prev_url: info?.url || "" }));
        const adminId = parseAdminChatId(env);
        if (adminId !== null) {
          await tgApi(env, "sendMessage", {
            chat_id: adminId,
            text: "🛠️ <b>[자동 복구]</b> Telegram Webhook 설정이 유실되어 자동으로 재등록했습니다.",
            parse_mode: "HTML",
          }).catch(() => {});
        }
      }
    } catch (err) {
      console.warn(JSON.stringify({ event: "webhook_check_error", message: String(err) }));
    }
  }

  console.log(JSON.stringify({ event: "daily_maintenance", pruned, webhookHealed }));
  return { ok: true, pruned, webhookHealed };
}

export async function runScheduledDispatch(env, nowDate = new Date()) {
  const kst = new Date(nowDate.getTime() + 9 * 3600 * 1000);
  const kstHour = kst.getUTCHours();
  const kstMinute = kst.getUTCMinutes();

  if (kstHour === 4 && kstMinute === 0) {
    await runDailyMaintenance(env, nowDate).catch((err) => {
      console.warn(JSON.stringify({ event: "daily_maintenance_error", message: String(err) }));
    });
  }

  if (kstHour >= 0 && kstHour < 7 && kstMinute % 20 !== 0) {
    console.log(JSON.stringify({ event: "cron_dispatch", skipped: true, reason: "quiet_hours", kstHour, kstMinute }));
    return { ok: true, skipped: true, reason: "quiet_hours", kstHour, kstMinute };
  }

  const ghToken = String(env?.GH_DISPATCH_TOKEN || "").trim();
  if (!ghToken) {
    console.warn(JSON.stringify({ event: "cron_dispatch", skipped: true, reason: "missing_gh_token" }));
    return { ok: false, skipped: true, reason: "missing_gh_token" };
  }

  const owner = String(env?.GH_REPO_OWNER || "produckyou-design").trim();
  const repo = String(env?.GH_REPO_NAME || "coupang-hotdeal-bot").trim();
  const wf = String(env?.GH_WORKFLOW_FILE || "deal_pusher.yml").trim();
  const dispatchUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${wf}/dispatches`;

  let res = null;
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      res = await fetch(dispatchUrl, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${ghToken}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "hotpang-click-worker",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main" }),
      });
      if (res.ok || (res.status !== 429 && res.status < 500)) {
        break;
      }
      if (attempt === 0) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    } catch (err) {
      lastErr = err;
      if (attempt === 0) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    }
  }

  const ok = Boolean(res && res.ok);
  const status = res ? res.status : 0;
  const nowMs = nowDate.getTime();
  const prevStreakRaw = await readChannelMeta(env, "dispatch_fail_streak");
  const prevStreak = Number.parseInt(prevStreakRaw || "0", 10) || 0;

  if (ok) {
    if (prevStreak > 0) {
      await writeChannelMeta(env, "dispatch_fail_streak", "0", nowDate.toISOString());
    }
    if (prevStreak >= 3) {
      const adminId = parseAdminChatId(env);
      if (adminId !== null && String(env?.TELEGRAM_BOT_TOKEN || "").trim()) {
        await tgApi(env, "sendMessage", {
          chat_id: adminId,
          text: "✅ <b>[자동 복구]</b> GitHub Actions 스케줄 트리거가 정상 복구되었습니다.",
          parse_mode: "HTML",
        }).catch(() => {});
      }
    }
  } else {
    const nextStreak = prevStreak + 1;
    await writeChannelMeta(env, "dispatch_fail_streak", String(nextStreak), nowDate.toISOString());
    if (nextStreak >= 3) {
      const authHint =
        status === 401 || status === 403
          ? "\n• <code>GH_DISPATCH_TOKEN</code> 만료 또는 권한 오류일 수 있습니다. PAT 갱신을 확인하세요."
          : "";
      const errDetail = lastErr ? ` (${String(lastErr)})` : ` (HTTP ${status})`;
      await sendWatchdogAlert(
        env,
        `🚨 <b>[자동 감시 경고]</b> GitHub Actions 스케줄 호출이 <b>${nextStreak}회 연속</b> 실패했습니다.${errDetail}${authHint}`,
        nowMs
      );
    }
  }

  // Stale runner watchdog (>4 hours since last flyer_status report)
  try {
    const flyerStatus = await readFlyerStatus(env);
    if (flyerStatus?.updated_at) {
      const updMs = Date.parse(String(flyerStatus.updated_at));
      if (!Number.isNaN(updMs) && nowMs - updMs > 4 * 3600 * 1000) {
        await sendWatchdogAlert(
          env,
          "⚠️ <b>[자동 감시 경고]</b> 핫딜 수집기(GitHub Actions)가 <b>4시간 이상</b> 실행 보고가 없습니다. Actions 로그를 확인해 주세요.",
          nowMs
        );
      }
    }
  } catch (err) {
    console.warn("stale watchdog check failed", String(err));
  }

  console.log(JSON.stringify({ event: "cron_dispatch", ok, status, kstHour, kstMinute }));
  return { ok, status, kstHour, kstMinute };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const method = String(request.method || "GET").toUpperCase();
    const path = url.pathname;

    if (method === "GET" && path === "/health") {
      return jsonResponse({ ok: true, service: "hotpang-click" });
    }
    if (method === "POST" && path === "/telegram/webhook") {
      return handleTelegramWebhook(request, env, ctx, url.origin);
    }
    if (path === "/api/public/feed") {
      if (method === "OPTIONS") {
        return new Response(null, { status: 204, headers: publicCorsHeaders("public, max-age=86400") });
      }
      if (method === "GET") return handlePublicFeed(env, url);
    }
    if (method === "POST" && path === "/api/links") {
      return handleCreateLink(request, env, url.origin);
    }
    if (method === "GET" && path === "/api/stats") {
      return handleStats(request, env, url);
    }
    if (path === "/api/channel/members") {
      if (method === "GET") return handleChannelMembersGet(request, env);
      if (method === "POST") return handleChannelMembersPost(request, env);
    }
    if (path === "/api/channel/status") {
      if (method === "GET") return handleChannelStatusGet(request, env);
      if (method === "POST") return handleChannelStatusPost(request, env);
    }
    if (path === "/api/channel/funnel") {
      if (method === "GET") return handleChannelFunnelGet(request, env, url);
      if (method === "POST") return handleChannelFunnelPost(request, env);
    }
    if (path === "/api/sales/state") {
      if (method === "GET") return handleSalesStateGet(request, env);
      if (method === "POST") return handleSalesStatePost(request, env);
    }
    if (method === "POST" && path === "/api/posts") {
      return handleCreatePost(request, env);
    }
    if (method === "GET" && path === "/api/posts/active") {
      return handleGetActivePosts(request, env, url);
    }
    if (method === "POST" && path === "/api/posts/update") {
      return handleUpdatePost(request, env);
    }
    if (path === "/api/alerts") {
      if (method === "GET") return handleGetAlerts(request, env, url);
      if (method === "POST") return handleCreateAlert(request, env, url.origin);
      if (method === "DELETE") return handleDeleteAlert(request, env, url);
    }
    if (method === "POST" && path === "/api/alerts/prefs") {
      return handleAlertPrefs(request, env);
    }
    if (method === "POST" && path === "/api/alerts/hits") {
      return handleAlertHits(request, env);
    }
    if (method === "GET" && path === "/api/alerts/recent") {
      return handleRecentAlertMatches(request, env, url);
    }
    if (method === "GET" && path === "/api/alerts/stats") {
      return handleAlertStats(request, env);
    }
    if ((method === "GET" || method === "HEAD") && path.startsWith("/r/")) {
      const code = path.slice(3).split("/")[0];
      return handleRedirect(request, env, ctx, code);
    }

    return jsonResponse({ ok: false, error: "not_found" }, 404);
  },

  async scheduled(controller, env, ctx) {
    const scheduledMs = controller?.scheduledTime || Date.now();
    const job = runScheduledDispatch(env, new Date(scheduledMs)).catch((err) => {
      console.error(JSON.stringify({ event: "cron_dispatch_error", message: String(err) }));
    });
    if (ctx && typeof ctx.waitUntil === "function") {
      ctx.waitUntil(job);
    } else {
      await job;
    }
  },
};
