import {
  createDeeplink,
  formatChannelSplitBlock,
  formatOrderSourceBadge,
  getCommissionReport,
  getOrderReport,
  searchProducts,
  summarizePartnerStats,
} from "./coupang_api.js";
import {
  compareOffers,
  createPad,
  describeOffer,
  deserializeOffer,
  formatRate,
  naturalOffer,
  padApply,
  padScreen,
  parseOffer,
  serializeOffer,
  waterRankings,
} from "./unit_compare.js";

const ALERT_PRESETS = ["생수", "닭가슴살", "에어팟", "라면", "물티슈", "커피"];
const SEARCH_PRESETS = ["생수", "세제", "기저귀", "커피", "휴지", "간식"];
const SOURCE_KR_MAP = {
  arca: "아카라이브",
  ppomppu: "뽐뿌",
  ruliweb: "루리웹",
  dealbada: "딜바다",
  clien: "클리앙",
  goldbox: "골드박스",
};
const JOINED_STATUSES = new Set(["member", "administrator", "creator"]);
const LEFT_STATUSES = new Set(["left", "kicked"]);

export function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function sourceKr(code) {
  const key = String(code || "").trim().toLowerCase();
  return SOURCE_KR_MAP[key] || key || "기타";
}

function kstDateObj(nowDate = new Date()) {
  return new Date(nowDate.getTime() + 9 * 3600 * 1000);
}

function formatKstYmd(nowDate = new Date()) {
  const k = kstDateObj(nowDate);
  const y = k.getUTCFullYear();
  const m = String(k.getUTCMonth() + 1).padStart(2, "0");
  const d = String(k.getUTCDate()).padStart(2, "0");
  return `${y}${m}${d}`;
}

function formatKstMonthStartYmd(nowDate = new Date()) {
  const k = kstDateObj(nowDate);
  return `${k.getUTCFullYear()}${String(k.getUTCMonth() + 1).padStart(2, "0")}01`;
}

function formatKstHm(isoStr) {
  try {
    const dt = new Date(String(isoStr || ""));
    if (Number.isNaN(dt.getTime())) return "";
    const k = kstDateObj(dt);
    const m = String(k.getUTCMonth() + 1).padStart(2, "0");
    const d = String(k.getUTCDate()).padStart(2, "0");
    const hh = String(k.getUTCHours()).padStart(2, "0");
    const mm = String(k.getUTCMinutes()).padStart(2, "0");
    return `${m}/${d} ${hh}:${mm}`;
  } catch {
    return "";
  }
}

export function parseAdminChatId(env) {
  const raw = String(env?.TELEGRAM_ADMIN_CHAT_ID ?? "").trim();
  if (!raw || !/^-?\d+$/.test(raw)) return null;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isSafeInteger(parsed) || parsed === 0) return null;
  return parsed;
}

export function isAdmin(env, uid) {
  const adminId = parseAdminChatId(env);
  return adminId !== null && Number(uid) === adminId;
}

export function isAdminUpdate(env, user, chat) {
  if (parseAdminChatId(env) === null) return false;
  if (user && isAdmin(env, user.id)) return true;
  if (chat && isAdmin(env, chat.id)) return true;
  return false;
}

function normalizeKeyword(raw) {
  return String(raw || "").trim().replace(/\s+/g, "").toLowerCase();
}

function sanitizeExcludeTermsList(raw) {
  const parts = Array.isArray(raw) ? raw : String(raw || "").split(",");
  const clean = [];
  const seen = new Set();
  for (const p of parts) {
    const tok = String(p || "").trim().replace(/^-+/, "").replace(/\s+/g, " ");
    const norm = normalizeKeyword(tok);
    if (!norm || seen.has(norm)) continue;
    seen.add(norm);
    clean.push(tok.slice(0, 20));
    if (clean.length >= 8) break;
  }
  return clean;
}

function formatWonCompact(price) {
  if (price <= 0) return "";
  if (price >= 10000 && price % 10000 === 0) return `${Math.floor(price / 10000)}만↓`;
  if (price >= 10000 && price % 1000 === 0) return `${(price / 10000).toFixed(1)}만↓`;
  return `${price.toLocaleString("en-US")}원↓`;
}

export function formatAlertConditionBadge(row) {
  const kw = String(row?.keyword || "").trim();
  const parts = [];
  const maxP = Number.parseInt(String(row?.max_price || 0), 10) || 0;
  const minD = Number.parseInt(String(row?.min_discount || 0), 10) || 0;
  const excl = sanitizeExcludeTermsList(row?.exclude_terms || "");
  if (maxP > 0) parts.push(formatWonCompact(maxP));
  if (minD > 0) parts.push(`${minD}%↑`);
  for (const ex of excl.slice(0, 2)) parts.push(`-${ex}`);
  if (excl.length > 2) parts.push(`외 ${excl.length - 2}제외`);
  const dispKw = kw.toLowerCase().startsWith("pid:") ? `📉 상품추적 #${kw.slice(4)}` : kw;
  return parts.length > 0 ? `${dispKw} (${parts.join(" · ")})` : dispKw;
}

const DISCOUNT_FILTER_RE = /(?:(?<=^)|(?<=\s))(?:>=\s*(\d{1,2})\s*%|(\d{1,2})\s*%\s*(?:이상|↑|\+|할인|초과))(?=\s|$)/i;
const MAN_PRICE_FILTER_RE = /(?:(?<=^)|(?<=\s))(?:(?:~|<=|최대)\s*(\d+(?:\.\d+)?)\s*만\s*원?|(\d+(?:\.\d+)?)\s*만\s*원?\s*(?:이하|미만|까지|↓))(?=\s|$)/;
const WON_PRICE_FILTER_RE = /(?:(?<=^)|(?<=\s))(?:(?:~|<=|최대)\s*(\d[\d,]{2,})\s*원?|(\d[\d,]{2,})\s*(?:원\s*(?:이하|미만|까지|↓)|(?:이하|미만|까지|↓)))(?=\s|$)/;
const EXCLUDE_TERM_RE = /(?:(?<=^)|(?<=\s))-([^\s,]+)/g;

export function parseAlertInput(rawText) {
  let text = String(rawText || "").trim().replace(/\s+/g, " ");
  let excludeTerms = [];
  let minDiscount = 0;
  let maxPrice = 0;

  const exclFound = Array.from(text.matchAll(EXCLUDE_TERM_RE)).map((m) => m[1]);
  if (exclFound.length > 0) {
    excludeTerms = sanitizeExcludeTermsList(exclFound);
    text = text.replace(EXCLUDE_TERM_RE, " ");
  }
  const mDisc = DISCOUNT_FILTER_RE.exec(text);
  if (mDisc) {
    const dVal = Number.parseInt(mDisc[1] || mDisc[2], 10);
    if (dVal >= 5 && dVal <= 90) minDiscount = dVal;
    text = text.replace(DISCOUNT_FILTER_RE, " ");
  }
  const mMan = MAN_PRICE_FILTER_RE.exec(text);
  if (mMan) {
    const pVal = Math.round(Number.parseFloat(mMan[1] || mMan[2]) * 10000);
    if (pVal >= 1000 && pVal <= 100000000) maxPrice = pVal;
    text = text.replace(MAN_PRICE_FILTER_RE, " ");
  }
  if (maxPrice === 0) {
    const mWon = WON_PRICE_FILTER_RE.exec(text);
    if (mWon) {
      const wVal = Number.parseInt((mWon[1] || mWon[2] || "").replaceAll(",", ""), 10);
      if (wVal >= 1000 && wVal <= 100000000) maxPrice = wVal;
      text = text.replace(WON_PRICE_FILTER_RE, " ");
    }
  }
  const keyword = text.trim().replace(/\s+/g, " ");
  return {
    keyword,
    keyword_norm: normalizeKeyword(keyword),
    max_price: maxPrice,
    min_discount: minDiscount,
    exclude_terms: excludeTerms,
  };
}

function formatClickStatsTelegram(stats, topN = 5) {
  const today = stats?.today || {};
  const period = stats?.period_total || {};
  const days = Number(stats?.days || 7);
  const lines = [
    "⚡ <b>실시간 클릭 현황</b>",
    `• 오늘 클릭: <b>${Number(today.clicks || 0).toLocaleString("en-US")}회</b> (상품 ${Number(today.unique_links || 0).toLocaleString("en-US")}개)`,
    `• 최근 ${days}일 누적: <b>${Number(period.clicks || 0).toLocaleString("en-US")}회</b> (등록 링크 ${Number(period.total_links || 0).toLocaleString("en-US")}개)`,
  ];
  const srcRows = (stats?.by_source_today?.length ? stats.by_source_today : stats?.by_source_period) || [];
  const srcParts = srcRows
    .slice(0, 5)
    .filter((r) => Number(r.clicks || 0) > 0)
    .map((r) => `${esc(sourceKr(r.source))} ${Number(r.clicks).toLocaleString("en-US")}회`);
  if (srcParts.length > 0) {
    const tag = stats?.by_source_today?.length ? "오늘 출처별" : `${days}일 출처별`;
    lines.push(`• ${tag}: ${srcParts.join(" · ")}`);
  }
  const topRows = (stats?.top_today?.length ? stats.top_today : stats?.top_period || []).slice(0, Math.max(1, topN));
  if (topRows.length > 0) {
    const topLabel = stats?.top_today?.length ? "오늘 인기 클릭" : `최근 ${days}일 인기 클릭`;
    lines.push(`• <b>${topLabel} TOP</b>`);
    topRows.forEach((item, idx) => {
      let rawTitle = String(item.title || item.code || "상품").trim();
      if (rawTitle.length > 26) rawTitle = `${rawTitle.slice(0, 25).trimEnd()}…`;
      const clicksN = Number(item.clicks || 0);
      const srcName = sourceKr(item.source);
      const saleP = Number(item.sale_price || 0);
      const pricePart = saleP > 0 ? ` · ${saleP.toLocaleString("en-US")}원` : "";
      lines.push(`  ${idx + 1}. ${esc(rawTitle)} (${esc(srcName)}${pricePart}) — <b>${clicksN.toLocaleString("en-US")}회</b>`);
    });
  }
  return lines.join("\n");
}

function formatAlertStatsTelegram(stats) {
  const lines = [
    "🔔 <b>맞춤 키워드 알림 현황</b>",
    `• 구독자: <b>${Number(stats?.total_users || 0).toLocaleString("en-US")}명</b> (일시정지 ${Number(stats?.paused_users || 0).toLocaleString("en-US")}명)`,
    `• 등록 키워드: <b>${Number(stats?.total_keywords || 0).toLocaleString("en-US")}개</b> | 누적 DM 발송: <b>${Number(stats?.total_hits || 0).toLocaleString("en-US")}건</b>`,
  ];
  const topRows = (stats?.top_keywords || []).slice(0, 10);
  if (topRows.length > 0) {
    lines.push("\n🏆 <b>인기 등록 키워드 TOP</b>");
    topRows.forEach((r, idx) => {
      const kw = String(r.keyword || "").trim();
      const subs = Number(r.subscribers || 0);
      const hits = Number(r.hits || 0);
      lines.push(`  ${idx + 1}. <code>${esc(kw)}</code> — ${subs.toLocaleString("en-US")}명 (알림 ${hits.toLocaleString("en-US")}회)`);
    });
  }
  return lines.join("\n");
}

function formatMemberStatsLine(stats) {
  const cnt = Number(stats?.count || 0);
  if (!stats || cnt <= 0) return "";
  const net = Number(stats?.today_net || 0);
  const netStr = net > 0 ? `+${net.toLocaleString("en-US")}명` : net < 0 ? `${net.toLocaleString("en-US")}명` : "변동 없음";
  return `👥 <b>채널 구독자</b>: <b>${cnt.toLocaleString("en-US")}명</b> (오늘 ${netStr})`;
}

export function formatBotMemberGrowthDm(res) {
  const cnt = Number(res?.count || 0);
  const delta = Number(res?.delta || 0);
  const todayNet = Number(res?.today_net || 0);
  const milestone = Number(res?.milestone || 0);
  const netStr = todayNet >= 0 ? `+${todayNet.toLocaleString("en-US")}명` : `${todayNet.toLocaleString("en-US")}명`;
  const lines = [`🎉 <b>[오늘의 핫딜 구독자 +${delta.toLocaleString("en-US")}]</b> 현재 <b>${cnt.toLocaleString("en-US")}명</b>!`];
  if (milestone > 0) lines.push(`🎊 <b>${milestone.toLocaleString("en-US")}명 돌파!</b>`);
  lines.push(`• 오늘 순증: <b>${netStr}</b>`);
  return lines.join("\n");
}

export async function tgApi(env, method, payload = {}) {
  const token = String(env?.TELEGRAM_BOT_TOKEN || "").trim();
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN missing");
  const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  if (!res.ok || data.ok === false) {
    throw new Error(`Telegram ${method} failed (${res.status}): ${data.description || res.statusText}`);
  }
  return data.result;
}

function defaultSession() {
  return {
    mode: "",
    compare_first: null,
    has_compare_session: false,
    pad: null,
    last_offer: null,
    offer_message: null,
  };
}

async function loadSession(env, chatId) {
  try {
    const row = await env.DB.prepare("SELECT state_json FROM bot_sessions WHERE chat_id = ? LIMIT 1")
      .bind(String(chatId))
      .first();
    if (!row?.state_json) return defaultSession();
    return { ...defaultSession(), ...JSON.parse(String(row.state_json)) };
  } catch {
    return defaultSession();
  }
}

async function saveSession(env, chatId, session) {
  try {
    await env.DB.prepare(
      `INSERT INTO bot_sessions (chat_id, state_json, updated_at)
       VALUES (?, ?, ?)
       ON CONFLICT(chat_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at`
    )
      .bind(String(chatId), JSON.stringify(session), new Date().toISOString())
      .run();
  } catch (err) {
    console.error("saveSession failed", String(err));
  }
}

function clearCompare(session) {
  session.compare_first = null;
  session.has_compare_session = false;
  session.pad = null;
}

function buildRemoteKeyboard() {
  return {
    keyboard: [
      [{ text: "📰 전단지 현황" }, { text: "⚡ 실시간 클릭" }],
      [{ text: "📊 리포트" }, { text: "🔍 검색" }],
      [{ text: "⚖️ 단가 비교" }, { text: "🔔 키워드 알림" }],
    ],
    resize_keyboard: true,
    is_persistent: true,
  };
}

function channelButtons(env) {
  const pub = String(env?.TELEGRAM_PUBLIC_CHANNEL_ID || "@HOTpang911").trim();
  if (/^@[A-Za-z0-9_]+$/.test(pub)) {
    return [{ text: "전단지 열기 ↗", url: `https://t.me/${pub.slice(1)}` }];
  }
  return [];
}

function safeCbStr(prefix, value, maxBytes = 64) {
  const raw = String(value || "").trim();
  const enc = new TextEncoder();
  const budget = Math.max(1, maxBytes - enc.encode(prefix).length);
  let out = "";
  let used = 0;
  for (const ch of raw) {
    const b = enc.encode(ch).length;
    if (used + b > budget) break;
    out += ch;
    used += b;
  }
  return prefix + out;
}

function formatRecentMatchesBlock(matches, header = "🔎 <b>최근 채널에 올라온 관련 핫딜</b>") {
  if (!matches || matches.length === 0) return [];
  const lines = ["", header];
  matches.slice(0, 5).forEach((m, idx) => {
    let title = String(m.title || "상품").trim();
    if (title.length > 32) title = `${title.slice(0, 31).trimEnd()}…`;
    const url = String(m.short_url || "").trim();
    const saleP = Number(m.sale_price || 0);
    const discR = Number(m.discount_rate || 0);
    const rateS = discR > 0 ? `${discR}%↓ ` : "";
    const priceS = saleP > 0 ? ` (${rateS}${saleP.toLocaleString("en-US")}원)` : "";
    if (url) {
      lines.push(`  ${idx + 1}. <a href="${esc(url)}">${esc(title)}</a>${esc(priceS)}`);
    } else {
      lines.push(`  ${idx + 1}. ${esc(title)}${esc(priceS)}`);
    }
  });
  return lines;
}

async function replyTo(env, chatId, text, extra = {}) {
  return tgApi(env, "sendMessage", { chat_id: chatId, text, ...extra });
}

async function showMenu(env, chatId, session) {
  session.mode = "";
  const links = channelButtons(env);
  let text = "🍯 꿀템봇 · 관리자 메뉴";
  if (links.length > 0) text += `\n<a href="${links[0].url}">핫딜 전단지 열기 ↗</a>`;
  text += "\n‘생수 2L’를 보내면 묶음별 가격을 찾아드립니다.";
  await replyTo(env, chatId, text, {
    reply_markup: buildRemoteKeyboard(),
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });
}

async function shopperMenu(env, chatId) {
  const rows = [
    [{ text: "🔔 맞춤 키워드 알림", callback_data: "alert:menu" }],
    [{ text: "⚖️ 배송비 포함 단가 비교", callback_data: "compare:start" }],
  ];
  const links = channelButtons(env);
  if (links.length > 0) rows.push(links);
  await replyTo(
    env,
    chatId,
    "🍯 원하는 상품 키워드를 등록하면 핫딜이 뜰 때 DM으로 바로 알려드려요.\n‘생수 2L’를 보내면 묶음별 가격도 찾아드립니다.",
    { reply_markup: { inline_keyboard: rows } }
  );
}

async function searchMenu(env, chatId, session) {
  session.mode = "search_wait";
  const btns = SEARCH_PRESETS.map((s) => ({ text: s, callback_data: `menu:find:${s}` }));
  await replyTo(env, chatId, "검색할 품목을 고르세요. 다른 상품은 이름을 입력해도 됩니다.", {
    reply_markup: { inline_keyboard: [btns.slice(0, 3), btns.slice(3)] },
  });
}

async function flyerStatus(env, chatId, user, chat, session, dbOps) {
  if (!isAdminUpdate(env, user, chat)) return;
  if (session.mode === "alert_wait") session.mode = "";
  const st = await dbOps.readFlyerStatus(env);
  const today = formatKstYmd();
  let sentText = "기록 확인 불가";
  let waitingText = "기록 확인 불가";
  if (st) {
    const sent = st.date === today ? Number(st.community_sent || 0) : 0;
    sentText = `${sent.toLocaleString("en-US")}건`;
    waitingText = `${Number(st.waiting || 0).toLocaleString("en-US")}건`;
  }
  await replyTo(
    env,
    chatId,
    `📰 오늘 전단지 현황 · 한국시간\n\n오늘 송출한 커뮤니티 상품 ${sentText}\n현재 전단지 대기 상품 ${waitingText}`,
    {
      reply_markup: {
        inline_keyboard: [[{ text: "🔄 다시 조회", callback_data: "menu:flyers" }, ...channelButtons(env)]],
      },
    }
  );
}

function addAlertMessage(env, origin, uid, rawKw, dbOps, parseNatural = true, explicitMaxPrice = 0) {
  let parsed;
  if (parseNatural) {
    parsed = parseAlertInput(rawKw);
  } else {
    const clean = String(rawKw || "").trim().replace(/\s+/g, " ");
    parsed = { keyword: clean, keyword_norm: normalizeKeyword(clean), max_price: 0, min_discount: 0, exclude_terms: [] };
  }
  const kw = parsed.keyword;
  const maxPrice = explicitMaxPrice > 0 ? explicitMaxPrice : parsed.max_price;
  const minDisc = parsed.min_discount;
  const excl = parsed.exclude_terms || [];
  if (!kw || normalizeKeyword(kw).length < 2) {
    return Promise.resolve({ ok: false, message: "키워드는 2~24자로 입력해 주세요.", recent_matches: [] });
  }
  return dbOps.createOrUpdateAlert(env, origin, String(uid), kw, maxPrice, minDisc, excl.join(", ")).then((res) => {
    if (!res.ok) {
      if (res.error === "limit_exceeded") {
        return { ok: false, message: "키워드는 최대 10개까지 등록할 수 있어요. 기존 키워드를 지운 뒤 다시 등록해 주세요.", recent_matches: [] };
      }
      if (res.error === "invalid_keyword") {
        return { ok: false, message: "키워드는 2~24자로 입력해 주세요.", recent_matches: [] };
      }
      return { ok: false, message: "일시적인 오류로 등록하지 못했습니다.", recent_matches: [] };
    }
    const label = formatAlertConditionBadge(res);
    let msg;
    if (res.already_exists && res.updated) msg = `조건을 업데이트했어요: '${label}'`;
    else if (res.already_exists) msg = `이미 등록된 키워드예요: '${label}'`;
    else msg = `알림 등록 완료: '${label}' (${Number(res.count || 1)}/10)`;
    return { ...res, ok: true, message: msg };
  });
}

async function alertMenu(env, chat, uid, session, dbOps, { notice = "", recentMatches = null, editMessageObj = null } = {}) {
  if (chat?.type !== "private") {
    await replyTo(env, chat.id, "꿀템봇과의 개인 대화에서 /alert 를 입력해 주세요.");
    return;
  }
  clearCompare(session);
  session.mode = "alert_wait";

  const { alerts: rowsData, prefs } = await dbOps.listUserAlerts(env, String(uid));
  const keywords = rowsData.map((r) => String(r.keyword || "").trim()).filter(Boolean);
  const existingNorms = new Set(keywords.map((k) => normalizeKeyword(k)));
  const isPaused = Number(prefs.paused || 0) === 1;
  const quietNight = Number(prefs.quiet_night ?? 1) === 1;
  const statusStr = isPaused ? "⏸️ 일시정지됨" : "🟢 수신 중";
  const nightStr = quietNight ? "🌙 야간무음 ON(23~08시)" : "☀️ 야간무음 OFF";

  const lines = [];
  if (notice) lines.push(`ℹ️ ${esc(notice)}`, "");
  lines.push(`🔔 <b>맞춤 키워드 알림 (${keywords.length}/10개)</b> · ${statusStr} · ${nightStr}`);
  if (rowsData.length > 0) {
    for (const r of rowsData) {
      if (!r.keyword) continue;
      const badge = formatAlertConditionBadge(r);
      const hits = Number(r.hit_count || 0);
      const hitS = hits > 0 ? ` <i>(누적 ${hits}회)</i>` : "";
      lines.push(`• <code>${esc(badge)}</code>${hitS}`);
    }
  } else {
    lines.push("아직 등록된 키워드가 없어요.");
  }
  if (recentMatches?.length) lines.push(...formatRecentMatchesBlock(recentMatches));
  lines.push(
    "",
    "💡 <b>스마트 조건 입력법</b> (채팅으로 바로 입력)\n" +
      "• 단순 등록: <code>삼다수</code> 또는 <code>삼다수 2L</code>\n" +
      "• 가격·할인·제외: <code>에어팟 25만원이하 -케이스</code> / <code>닭가슴살 30%이상</code>\n" +
      "• 여러 개 한 번에: 쉼표(,)로 구분"
  );
  const text = lines.join("\n");

  const kbRows = [];
  const delBtns = [];
  for (const r of rowsData) {
    const kw = String(r.keyword || "").trim();
    if (!kw) continue;
    const rid = Number(r.id || 0);
    const shortLbl = kw.length > 10 ? `${kw.slice(0, 9)}…` : kw;
    const cb = rid > 0 ? `alert:delid:${rid}` : safeCbStr("alert:del:", kw);
    delBtns.push({ text: `❌ ${shortLbl}`, callback_data: cb });
  }
  for (let i = 0; i < delBtns.length; i += 2) kbRows.push(delBtns.slice(i, i + 2));

  const addBtns = ALERT_PRESETS
    .filter((p) => !existingNorms.has(normalizeKeyword(p)))
    .map((p) => ({ text: `+ ${p}`, callback_data: `alert:add:${p}` }));
  for (let i = 0; i < addBtns.length; i += 3) kbRows.push(addBtns.slice(i, i + 3));

  const ctrlRow = [];
  if (rowsData.length > 0) ctrlRow.push({ text: "🔍 최근 매칭", callback_data: "alert:recent" });
  ctrlRow.push({ text: isPaused ? "▶️ 알림 재개" : "⏸️ 일시정지", callback_data: "alert:toggle_pause" });
  ctrlRow.push({ text: quietNight ? "☀️ 야간무음 끄기" : "🌙 야간무음 켜기", callback_data: "alert:toggle_quiet" });
  kbRows.push(ctrlRow);

  if (isAdmin(env, uid)) {
    kbRows.push([{ text: "📊 구독자 키워드 통계", callback_data: "alert:stats" }]);
  }
  const footer = [];
  if (keywords.length > 0) footer.push({ text: "🗑️ 전체 삭제", callback_data: "alert:clear" });
  footer.push({ text: "🏠 메뉴", callback_data: "compare:home" });
  kbRows.push(footer);
  const markup = { inline_keyboard: kbRows };

  if (editMessageObj && !editMessageObj.photo) {
    try {
      await tgApi(env, "editMessageText", {
        chat_id: chat.id,
        message_id: editMessageObj.message_id,
        text,
        reply_markup: markup,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      });
      return;
    } catch {
      // Fall through to replyTo
    }
  }
  await replyTo(env, chat.id, text, {
    reply_markup: markup,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });
}

async function compareCmd(env, chat, session) {
  if (chat?.type !== "private") {
    await replyTo(env, chat.id, "꿀템봇과의 개인 대화에서 /compare 를 입력해 주세요.");
    return;
  }
  clearCompare(session);
  session.has_compare_session = true;
  session.mode = "";
  await replyTo(
    env,
    chat.id,
    "생수는 아래 버튼 하나로 비교하세요. 다른 상품은 ‘생수 2L 6병 5,400원’처럼 보내 주세요. 두 상품은 한 줄씩 보내면 됩니다.",
    {
      reply_markup: {
        inline_keyboard: [
          [{ text: "💧 생수 2L 자동 비교", callback_data: "compare:water" }],
          [{ text: "🔢 버튼으로 직접 입력", callback_data: "compare:pad" }, { text: "🏠 메뉴", callback_data: "compare:home" }],
        ],
      },
    }
  );
}

async function waterCompare(env, chat, session) {
  if (chat?.type !== "private") return;
  clearCompare(session);
  const waterKb = { inline_keyboard: [[{ text: "💧 생수 2L 자동 비교", callback_data: "compare:water" }]] };
  let products = null;
  let checked = "";
  try {
    const cachedRow = await env.DB.prepare("SELECT value FROM channel_meta WHERE key = 'water_rankings_cache' LIMIT 1").first();
    if (cachedRow?.value) {
      const parsed = JSON.parse(String(cachedRow.value));
      if (parsed.ts && Date.now() - Number(parsed.ts) < 300000 && Array.isArray(parsed.items)) {
        checked = String(parsed.checked || "");
        products = parsed.items.map((it) => [
          { num: BigInt(it.rate_num), den: BigInt(it.rate_den) },
          it.name,
          it.count,
          it.price,
          it.free,
          it.url,
        ]);
      }
    }
  } catch {
    products = null;
  }

  if (!products) {
    try {
      const response = await searchProducts(env, "생수 2L 12병", 10, "tgunit");
      if (String(response?.rCode) !== "0") throw new Error("조회 실패");
      products = waterRankings(response?.data?.productData || []);
      const k = kstDateObj();
      checked = `${String(k.getUTCHours()).padStart(2, "0")}:${String(k.getUTCMinutes()).padStart(2, "0")}`;
      const cachePayload = {
        ts: Date.now(),
        checked,
        items: products.map(([rate, name, count, price, free, url]) => ({
          rate_num: rate.num.toString(),
          rate_den: rate.den.toString(),
          name,
          count,
          price,
          free,
          url,
        })),
      };
      await env.DB.prepare(
        `INSERT INTO channel_meta (key, value, updated_at) VALUES ('water_rankings_cache', ?, ?)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
      )
        .bind(JSON.stringify(cachePayload), new Date().toISOString())
        .run()
        .catch(() => {});
    } catch {
      await replyTo(env, chat.id, "쿠팡 가격을 지금 조회할 수 없어요. 잠시 후 다시 눌러 주세요.", { reply_markup: waterKb });
      return;
    }
  }

  if (!products || products.length === 0) {
    await replyTo(env, chat.id, "검색 결과에 2L와 병 수가 함께 표시된 생수가 없어요. 상품 문구를 보내면 계산해 드려요.", {
      reply_markup: waterKb,
    });
    return;
  }

  const lines = [`💧 생수 2L 묶음 비교 · ${checked} 조회`, "검색 결과 중 1L당 상품값이 낮은 순서예요."];
  const buttons = [];
  products.forEach(([rate, name, count, price, free, url], i) => {
    const shipping = free ? "무료배송 표시" : "배송비 확인 필요";
    lines.push(
      `${i + 1}. ${name}\n${count}병 ${price.toLocaleString("en-US")}원 · 병당 ${formatRate({
        num: rate.num * 2n,
        den: rate.den,
      })} · 1L당 ${formatRate(rate)}\n${shipping}`
    );
    buttons.push([{ text: `${i + 1}. 상품 보기`, url }]);
  });
  lines.push("쿠팡 파트너스 활동으로 구매 시 일정액의 수수료를 제공받습니다.");
  await replyTo(env, chat.id, lines.join("\n\n"), { reply_markup: { inline_keyboard: buttons } });
}

function compareResultKb() {
  return {
    inline_keyboard: [[{ text: "⚖️ 다시 비교", callback_data: "compare:start" }, { text: "🏠 메뉴", callback_data: "compare:home" }]],
  };
}

async function compareInput(env, chatId, session, text) {
  const first = deserializeOffer(session.compare_first);
  try {
    const lines = String(text || "").split(/(?:[\r\n]+|\s+vs\.?\s+)/i).map((s) => s.trim()).filter(Boolean);
    if (lines.length > 2) throw new Error("상품은 한 번에 두 개까지, 한 줄에 하나씩 보내 주세요.");
    const offers = lines.map((s) => (/^[0-9,]+\s*원?\s*\//.test(s) && s.split("/").length - 1 === 2 ? parseOffer(s) : naturalOffer(s)));
    let result;
    if (offers.length === 2) {
      result = compareOffers(offers[0], offers[1]);
    } else if (!first) {
      const offer = offers[0];
      const inCompare = Boolean(session.has_compare_session);
      clearCompare(session);
      const sent = await replyTo(env, chatId, describeOffer(offer), {
        reply_markup: {
          inline_keyboard: [[{ text: "다른 상품과 비교", callback_data: "compare:next" }, { text: "🏠 메뉴", callback_data: "compare:home" }]],
        },
      });
      session.last_offer = serializeOffer(offer);
      session.offer_message = sent?.message_id || null;
      if (inCompare) {
        session.compare_first = serializeOffer(offer);
        session.has_compare_session = true;
      }
      return;
    } else {
      result = compareOffers(first, offers[0]);
    }
    clearCompare(session);
    await replyTo(env, chatId, result, { reply_markup: compareResultKb() });
  } catch (err) {
    await replyTo(env, chatId, `${err.message}\n다시 입력하거나 /cancel 로 취소하세요.`);
  }
}

async function searchCmd(env, chat, user, keyword) {
  if (!isAdminUpdate(env, user, chat)) return;
  const msg = await replyTo(env, chat.id, `🔍 '${keyword}' 검색 중...`);
  const msgId = msg?.message_id;
  try {
    const res = await searchProducts(env, keyword, 5, "tgsearch");
    const items = Array.isArray(res?.data?.productData) ? res.data.productData : [];
    if (items.length === 0) {
      if (msgId) await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: msgId, text: `❌ '${keyword}'에 대한 검색 결과가 없습니다.` });
      return;
    }
    if (msgId) {
      await tgApi(env, "editMessageText", {
        chat_id: chat.id,
        message_id: msgId,
        text: `🔎 <b>'${esc(keyword)}' 검색 결과 상위 5개</b>:`,
        parse_mode: "HTML",
      });
    }
    for (let i = 0; i < Math.min(5, items.length); i += 1) {
      const item = items[i];
      const pName = String(item.productName || "");
      const pPrice = Number.parseInt(String(item.productPrice || 0), 10) || 0;
      const pUrl = String(item.productUrl || "");
      const imgUrl = String(item.productImage || "");
      const caption = `[${i + 1}] <b>${esc(pName)}</b>\n💰 가격: <b>${pPrice.toLocaleString("en-US")}원</b>`;
      const markup = { inline_keyboard: [[{ text: "👉 쿠팡에서 보기", url: pUrl }]] };
      if (imgUrl) {
        try {
          await tgApi(env, "sendPhoto", { chat_id: chat.id, photo: imgUrl, caption, reply_markup: markup, parse_mode: "HTML" });
          continue;
        } catch {
          // Fall through to text message
        }
      }
      await replyTo(env, chat.id, caption, { reply_markup: markup, parse_mode: "HTML" });
    }
  } catch (err) {
    if (msgId) await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: msgId, text: `❌ 검색 중 오류 발생: ${err.message}` }).catch(() => {});
  }
}

async function clicksCmd(env, chat, user, origin, dbOps) {
  if (!isAdminUpdate(env, user, chat)) return;
  const markup = {
    inline_keyboard: [
      [{ text: "🔄 새로고침", callback_data: "menu:clicks" }, { text: "📊 파트너스 리포트", callback_data: "menu:report" }],
      [{ text: "📊 구독자 키워드 통계", callback_data: "alert:stats" }],
    ],
  };
  try {
    const [stats, alertStats, memberStats] = await Promise.all([
      dbOps.queryClickStats(env, origin, 7, 8),
      dbOps.queryAlertStats(env),
      dbOps.queryChannelMemberStats(env),
    ]);
    const k = kstDateObj();
    const nowStr = `${k.getUTCFullYear()}.${String(k.getUTCMonth() + 1).padStart(2, "0")}.${String(k.getUTCDate()).padStart(2, "0")} ${String(
      k.getUTCHours()
    ).padStart(2, "0")}:${String(k.getUTCMinutes()).padStart(2, "0")}`;
    const body = formatClickStatsTelegram(stats, 8);
    const recentLines = (stats.recent || []).slice(0, 5).map((r) => {
      const tStr = formatKstHm(r.clicked_at || "");
      const dev = Number(r.is_mobile || 0) ? "📱" : "💻";
      let title = String(r.title || r.code || "상품").trim();
      if (title.length > 22) title = `${title.slice(0, 21).trimEnd()}…`;
      return `  • ${tStr ? `${tStr} ` : ""}${dev} ${esc(title)}`;
    });
    const recentBlock = recentLines.length > 0 ? `\n\n🕒 <b>최근 클릭 내역</b>\n${recentLines.join("\n")}` : "";
    const alertBlock =
      `\n\n🔔 <b>키워드 구독</b>: ${Number(alertStats.total_users || 0).toLocaleString("en-US")}명 · ` +
      `키워드 ${Number(alertStats.total_keywords || 0).toLocaleString("en-US")}개 · ` +
      `누적 DM ${Number(alertStats.total_hits || 0).toLocaleString("en-US")}건`;
    const memberLine = formatMemberStatsLine(memberStats);
    const memberBlock = memberLine ? `\n${memberLine}` : "";
    await replyTo(env, chat.id, `${body}${recentBlock}${alertBlock}${memberBlock}\n\n📅 조회 기준: ${nowStr} KST`, {
      reply_markup: markup,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    });
  } catch {
    await replyTo(env, chat.id, "❌ 실시간 클릭 통계를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.", { reply_markup: markup });
  }
}

async function reportCmd(env, chat, user, origin, dbOps) {
  if (!isAdminUpdate(env, user, chat)) return;
  const msg = await replyTo(env, chat.id, "📊 채널별 파트너스 통계 조회 중...");
  const msgId = msg?.message_id;
  try {
    const now = new Date();
    const k = kstDateObj(now);
    const todayStr = formatKstYmd(now);
    const monthStart = formatKstMonthStartYmd(now);
    const weekStart = formatKstYmd(new Date(now.getTime() - 6 * 86400 * 1000));

    const [commMonthRes, commWeekRes, orderRes, clickStats, memberStats] = await Promise.all([
      getCommissionReport(env, monthStart, todayStr),
      getCommissionReport(env, weekStart, todayStr),
      getOrderReport(env, weekStart, todayStr),
      dbOps.queryClickStats(env, origin, 7, 5).catch(() => null),
      dbOps.queryChannelMemberStats(env).catch(() => null),
    ]);

    const mStats = summarizePartnerStats(commMonthRes?.data || [], orderRes?.data || []);
    const wStats = summarizePartnerStats(commWeekRes?.data || []);
    const wt = wStats.total;
    const mt = mStats.total;
    const kMonth = k.getUTCMonth() + 1;
    const kDay = k.getUTCDate();
    const channelBlock = formatChannelSplitBlock(mStats, `채널별 분리 집계 (${kMonth}월 누적)`);
    const clickBlock = clickStats ? `${formatClickStatsTelegram(clickStats, 3)}\n\n` : "";

    const taggedOrders = [...mStats.tagged_orders].sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
    const orderLines = taggedOrders.slice(0, 5).map((it) => {
      const dRaw = String(it.date || "");
      const dFmt = dRaw.length === 8 ? `${dRaw.slice(4, 6)}/${dRaw.slice(6, 8)}` : dRaw;
      const pname = String(it.productName || "상품").slice(0, 28);
      const qty = Number(it.quantity || 1);
      const g = Math.round(Number(it.gmv || 0));
      const c = Math.round(Number(it.commission || 0));
      const srcBadge = formatOrderSourceBadge(it.subId);
      return `• [${dFmt}·${esc(srcBadge)}] ${esc(pname)} x${qty} (<b>${g.toLocaleString("en-US")}원</b> / 수익 ${c.toLocaleString("en-US")}원)`;
    });
    const ordersBlock =
      orderLines.length > 0
        ? `<b>[최근 7일 주문 품목]</b>\n${orderLines.join("\n")}\n\n`
        : "<b>[최근 7일 주문 품목]</b>\n• 확정 주문 내역 없음\n\n";

    const dailyLines = [];
    if (mStats.latest_date) {
      const ld = mStats.latest_date;
      const lt = mStats.latest_total;
      const ltg = mStats.latest_telegram;
      const lpw = mStats.latest_poliwiki;
      const lFmt = ld.length === 8 ? `${ld.slice(0, 4)}.${ld.slice(4, 6)}.${ld.slice(6, 8)}` : ld;
      dailyLines.push(
        `📌 <b>최근 확정일 실적 (${lFmt})</b>`,
        `• 클릭/주문/취소: <b>${lt.clicks.toLocaleString("en-US")}회</b> / <b>${lt.orders.toLocaleString("en-US")}건</b> / ${lt.cancels.toLocaleString("en-US")}건`,
        `• 결제액 / 확정 수익: <b>${lt.gmv.toLocaleString("en-US")}원</b> / <b>${lt.commission.toLocaleString("en-US")}원</b>`
      );
      if (ltg.clicks + ltg.orders + lpw.clicks + lpw.orders > 0) {
        dailyLines.push(
          `• ✈️ 텔레그램: ${ltg.orders.toLocaleString("en-US")}건 (수익 ${ltg.commission.toLocaleString("en-US")}원) · 🌐 폴리위키: ${lpw.orders.toLocaleString("en-US")}건 (수익 ${lpw.commission.toLocaleString("en-US")}원)`
        );
      }
    } else {
      dailyLines.push("📌 <b>최근 확정일 실적</b>: 집계 내역 없음");
    }

    const liveParts = [];
    if (clickStats?.today) {
      liveParts.push(
        `⚡ 오늘 실시간 클릭(${kMonth}/${kDay}): <b>${Number(clickStats.today.clicks || 0).toLocaleString("en-US")}회</b> (상품 ${Number(
          clickStats.today.unique_links || 0
        ).toLocaleString("en-US")}개)`
      );
    }
    const mCnt = Number(memberStats?.count || 0);
    if (mCnt > 0) {
      const net = Number(memberStats?.today_net || 0);
      const netStr = net > 0 ? `+${net.toLocaleString("en-US")}명` : net < 0 ? `${net.toLocaleString("en-US")}명` : "변동 없음";
      liveParts.push(`👥 구독자 <b>${mCnt.toLocaleString("en-US")}명</b> (오늘 ${netStr})`);
    }
    if (liveParts.length > 0) {
      dailyLines.push(`• ${liveParts.join(" · ")}`);
    }
    const dailyBottomBlock = `━━━━━━━━━━━━━━━━━━\n${dailyLines.join("\n")}`;

    const nowLabel = `${k.getUTCFullYear()}.${String(kMonth).padStart(2, "0")}.${String(kDay).padStart(2, "0")} ${String(
      k.getUTCHours()
    ).padStart(2, "0")}:${String(k.getUTCMinutes()).padStart(2, "0")}`;
    const reportText =
      `📊 <b>[쿠팡 파트너스 채널별 통계]</b>\n` +
      `📅 조회 기준: ${nowLabel} KST\n\n` +
      `🗓️ <b>최근 7일 합계</b>\n` +
      `• 클릭/주문/취소: <b>${wt.clicks.toLocaleString("en-US")}회</b> / <b>${wt.orders.toLocaleString("en-US")}건</b> / ${wt.cancels.toLocaleString("en-US")}건\n` +
      `• 결제액 / 수익: ${wt.gmv.toLocaleString("en-US")}원 / <b>${wt.commission.toLocaleString("en-US")}원</b>\n\n` +
      `🗓️ <b>${kMonth}월 누적 (${kMonth}/1 ~ ${kMonth}/${kDay})</b>\n` +
      `• 클릭/주문/취소: <b>${mt.clicks.toLocaleString("en-US")}회</b> / <b>${mt.orders.toLocaleString("en-US")}건</b> / ${mt.cancels.toLocaleString("en-US")}건\n` +
      `• 결제액 / 확정 수익: ${mt.gmv.toLocaleString("en-US")}원 / <b>${mt.commission.toLocaleString("en-US")}원</b>\n\n` +
      `${channelBlock}\n\n` +
      `${clickBlock}` +
      `${ordersBlock}` +
      `※ <i>쿠팡 API 리포트는 익일 오후(D+1~D+2) 확정 집계됩니다.</i>\n\n` +
      `${dailyBottomBlock}`;

    if (msgId) await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: msgId, text: reportText, parse_mode: "HTML" });
    else await replyTo(env, chat.id, reportText, { parse_mode: "HTML" });
  } catch (err) {
    if (msgId) await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: msgId, text: `❌ 실적 조회 중 오류: ${esc(err.message)}` }).catch(() => {});
  }
}

export function isTargetPublicChannel(env, chat) {
  if (!chat) return false;
  const pub = String(env?.TELEGRAM_PUBLIC_CHANNEL_ID || "@HOTpang911").trim();
  if (pub.startsWith("@")) {
    return String(chat.username || "").trim().toLowerCase() === pub.slice(1).toLowerCase();
  }
  return String(chat.id ?? "") === pub;
}

async function handleChatMemberUpdate(cm, env, dbOps) {
  if (!cm || !isTargetPublicChannel(env, cm.chat)) return;
  const oldSt = String(cm.old_chat_member?.status || "");
  const newSt = String(cm.new_chat_member?.status || "");
  const joined = LEFT_STATUSES.has(oldSt) && JOINED_STATUSES.has(newSt);
  const left = JOINED_STATUSES.has(oldSt) && LEFT_STATUSES.has(newSt);
  if (!joined && !left) return;

  let cnt;
  try {
    cnt = await tgApi(env, "getChatMemberCount", { chat_id: cm.chat.id });
  } catch (err) {
    console.warn("getChatMemberCount failed", String(err));
    return;
  }
  if (typeof cnt !== "number") return;

  const res = await dbOps.recordChannelMemberCount(env, cnt);
  const adminId = parseAdminChatId(env);
  if (!res || adminId === null) return;
  if (Number(res.delta || 0) > 0) {
    const kstHour = kstDateObj().getUTCHours();
    const quiet = kstHour >= 0 && kstHour < 7;
    try {
      await tgApi(env, "sendMessage", {
        chat_id: adminId,
        text: formatBotMemberGrowthDm(res),
        parse_mode: "HTML",
        disable_notification: quiet,
      });
    } catch (err) {
      console.warn("member growth DM failed", String(err));
    }
  }
}

async function handleCallbackQuery(query, env, origin, dbOps) {
  const data = String(query.data || "");
  const msg = query.message;
  const chat = msg?.chat;
  const user = query.from;
  const uid = user?.id;
  if (!chat || !uid) {
    await tgApi(env, "answerCallbackQuery", { callback_query_id: query.id }).catch(() => {});
    return;
  }

  const session = await loadSession(env, chat.id);
  const answerCb = (text = "", showAlert = false) =>
    tgApi(env, "answerCallbackQuery", { callback_query_id: query.id, ...(text ? { text } : {}), ...(showAlert ? { show_alert: true } : {}) }).catch(() => {});

  try {
    if (data.startsWith("publish:")) {
      await answerCb("수동 게시 버튼은 종료됐습니다.", true);
      return;
    }

    if (data.startsWith("compare:")) {
      await answerCb();
      const sub = data.slice("compare:".length);
      if (sub === "start") {
        await compareCmd(env, chat, session);
      } else if (sub === "water") {
        await waterCompare(env, chat, session);
      } else if (sub === "pad") {
        if (chat.type !== "private") return;
        session.compare_first = null;
        session.has_compare_session = true;
        session.pad = createPad();
        const [txt, markup] = padScreen(session.pad, null);
        await replyTo(env, chat.id, txt, { reply_markup: markup });
      } else if (sub === "next") {
        if (chat.type !== "private" || !session.last_offer || msg?.message_id !== session.offer_message) {
          await replyTo(env, chat.id, "가장 최근 계산 결과에서 눌러 주세요.");
          return;
        }
        session.pad = null;
        session.compare_first = session.last_offer;
        session.has_compare_session = true;
        await replyTo(env, chat.id, "비교할 상품 문구를 보내 주세요. 예) 생수 500ml 24병 7,200원");
      } else if (sub === "home") {
        if (chat.type === "private") {
          clearCompare(session);
          session.mode = "";
          if (!isAdmin(env, chat.id)) await shopperMenu(env, chat.id);
          else await showMenu(env, chat.id, session);
        } else if (isAdminUpdate(env, user, chat)) {
          await showMenu(env, chat.id, session);
        }
      }
      await saveSession(env, chat.id, session);
      return;
    }

    if (/^pad:[0-9a-f]{8}:/.test(data)) {
      const parts = data.split(":");
      const token = parts[1];
      const action = parts.slice(2).join(":");
      const pad = session.pad;
      if (chat.type !== "private" || !pad || token !== pad.token) {
        await answerCb("이전 화면이에요. 가장 최근 버튼을 사용하거나 메뉴에서 다시 시작하세요.");
        return;
      }
      const first = deserializeOffer(session.compare_first);
      const [oldText, oldMarkup] = padScreen(pad, first);
      let offer = null;
      try {
        offer = padApply(pad, action, first);
      } catch (err) {
        await answerCb(err.message);
        return;
      }
      await answerCb();
      if (offer && first) {
        const result = compareOffers(first, offer);
        clearCompare(session);
        await tgApi(env, "editMessageText", {
          chat_id: chat.id,
          message_id: msg.message_id,
          text: result,
          reply_markup: compareResultKb(),
        });
      } else {
        let nextFirst = first;
        if (offer) {
          session.compare_first = serializeOffer(offer);
          session.has_compare_session = true;
          nextFirst = offer;
          session.pad = createPad();
        }
        const [newText, newMarkup] = padScreen(session.pad, nextFirst);
        if (newText !== oldText || JSON.stringify(newMarkup) !== JSON.stringify(oldMarkup)) {
          await tgApi(env, "editMessageText", {
            chat_id: chat.id,
            message_id: msg.message_id,
            text: newText,
            reply_markup: newMarkup,
          });
        }
      }
      await saveSession(env, chat.id, session);
      return;
    }

    if (data.startsWith("alert:")) {
      if (chat.type !== "private") {
        await answerCb();
        return;
      }
      let notice = "";
      let recentMatches = null;
      if (data === "alert:menu") {
        await answerCb();
      } else if (data.startsWith("alert:watch:")) {
        const parts = data.split(":");
        const pid = (parts[2] || "").trim();
        const watchP = Number.parseInt(parts[3] || "0", 10) || 0;
        if (!pid || watchP < 1000) {
          await answerCb("상품 추적 정보가 올바르지 않습니다.");
          return;
        }
        const resW = await addAlertMessage(env, origin, uid, `pid:${pid}`, dbOps, false, watchP);
        await answerCb(resW.ok ? `${watchP.toLocaleString("en-US")}원 이하 하락 알림 등록 완료!` : resW.message);
        if (resW.ok) {
          await replyTo(
            env,
            chat.id,
            `📉 <b>상품 추가 하락 알림 등록 완료!</b>\n` +
              `• 상품코드 <code>#${esc(pid)}</code> 이(가) <b>${watchP.toLocaleString("en-US")}원 이하</b>로 내려가면 DM으로 바로 알려드릴게요.\n` +
              `• 관리 및 삭제는 /alert 에서 할 수 있습니다.`,
            { parse_mode: "HTML" }
          );
        }
        return;
      } else if (data.startsWith("alert:muteid:") || data.startsWith("alert:mute:")) {
        let removed = false;
        let doneMsg = "해당 키워드 알림을 껐어요.";
        if (data.startsWith("alert:muteid:")) {
          const rid = Number.parseInt(data.slice("alert:muteid:".length), 10) || 0;
          if (rid > 0) removed = await dbOps.deleteUserAlert(env, String(uid), { alertId: rid });
        } else {
          const kw = data.slice("alert:mute:".length);
          removed = await dbOps.deleteUserAlert(env, String(uid), { keyword: kw });
          doneMsg = `'${kw}' 키워드 알림을 껐어요.`;
        }
        await answerCb(removed ? doneMsg : "이미 삭제되었거나 찾지 못했어요.");
        if (removed) await replyTo(env, chat.id, `🔕 ${doneMsg}\n다시 관리하려면 /alert 를 입력해 주세요.`);
        return;
      } else if (data === "alert:recent") {
        await answerCb();
        const matches = await dbOps.findUserRecentMatches(env, origin, String(uid), 14, 5);
        const text = matches.length
          ? formatRecentMatchesBlock(matches, "🔍 <b>최근 14일 내 키워드 매칭 핫딜</b>").join("\n").trim()
          : "🔍 최근 14일간 내 키워드와 매칭된 채널 핫딜이 아직 없어요.";
        const markup = { inline_keyboard: [[{ text: "⬅️ 키워드 관리", callback_data: "alert:menu" }]] };
        if (msg && !msg.photo) {
          try {
            await tgApi(env, "editMessageText", {
              chat_id: chat.id,
              message_id: msg.message_id,
              text,
              reply_markup: markup,
              parse_mode: "HTML",
              disable_web_page_preview: true,
            });
            return;
          } catch {}
        }
        await replyTo(env, chat.id, text, { reply_markup: markup, parse_mode: "HTML", disable_web_page_preview: true });
        return;
      } else if (data === "alert:stats") {
        if (!isAdmin(env, uid)) {
          await answerCb("관리자 전용 메뉴입니다.", true);
          return;
        }
        await answerCb();
        const st = await dbOps.queryAlertStats(env);
        const text = st ? formatAlertStatsTelegram(st) : "❌ 키워드 통계를 불러오지 못했습니다.";
        const markup = {
          inline_keyboard: [[{ text: "🔄 새로고침", callback_data: "alert:stats" }, { text: "⬅️ 키워드 관리", callback_data: "alert:menu" }]],
        };
        if (msg && !msg.photo) {
          try {
            await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: msg.message_id, text, reply_markup: markup, parse_mode: "HTML" });
            return;
          } catch {}
        }
        await replyTo(env, chat.id, text, { reply_markup: markup, parse_mode: "HTML" });
        return;
      } else if (data === "alert:toggle_pause") {
        await answerCb();
        const { prefs } = await dbOps.listUserAlerts(env, String(uid));
        const nextPaused = Number(prefs.paused || 0) !== 1;
        await dbOps.updateUserAlertPrefs(env, String(uid), { paused: nextPaused });
        notice = nextPaused ? "키워드 알림을 일시정지했어요." : "키워드 알림 수신을 재개했어요.";
      } else if (data === "alert:toggle_quiet") {
        await answerCb();
        const { prefs } = await dbOps.listUserAlerts(env, String(uid));
        const nextQuiet = Number(prefs.quiet_night ?? 1) !== 1;
        await dbOps.updateUserAlertPrefs(env, String(uid), { quiet_night: nextQuiet });
        notice = nextQuiet ? "야간 무음(23~08시)을 켰어요." : "야간 무음(23~08시)을 껐어요.";
      } else if (data.startsWith("alert:add:")) {
        await answerCb();
        const resAdd = await addAlertMessage(env, origin, uid, data.slice("alert:add:".length), dbOps);
        notice = resAdd.message || "";
        recentMatches = resAdd.recent_matches?.length ? resAdd.recent_matches : null;
      } else if (data.startsWith("alert:delid:")) {
        await answerCb();
        const rid = Number.parseInt(data.slice("alert:delid:".length), 10) || 0;
        const removed = rid > 0 ? await dbOps.deleteUserAlert(env, String(uid), { alertId: rid }) : false;
        notice = removed ? "키워드 알림을 삭제했어요." : "삭제하지 못했습니다.";
      } else if (data.startsWith("alert:del:")) {
        await answerCb();
        const kw = data.slice("alert:del:".length);
        const removed = await dbOps.deleteUserAlert(env, String(uid), { keyword: kw });
        notice = removed ? `'${kw}' 알림을 삭제했어요.` : "삭제하지 못했습니다.";
      } else if (data === "alert:clear") {
        await answerCb();
        const removed = await dbOps.deleteUserAlert(env, String(uid), { removeAll: true });
        notice = removed ? "등록된 키워드를 모두 삭제했어요." : "전체 삭제에 실패했습니다.";
      } else {
        await answerCb();
      }
      await alertMenu(env, chat, uid, session, dbOps, { notice, recentMatches, editMessageObj: msg });
      await saveSession(env, chat.id, session);
      return;
    }

    if (data.startsWith("menu:")) {
      await answerCb();
      if (!isAdminUpdate(env, user, chat)) return;
      const action = data.slice("menu:".length);
      if (chat.type === "private") clearCompare(session);
      session.mode = "";
      if (action === "search") {
        await searchMenu(env, chat.id, session);
      } else if (action.startsWith("find:") && SEARCH_PRESETS.includes(action.slice(5))) {
        await searchCmd(env, chat, user, action.slice(5));
      } else if (action === "flyers") {
        await flyerStatus(env, chat.id, user, chat, session, dbOps);
      } else if (action === "clicks") {
        await clicksCmd(env, chat, user, origin, dbOps);
      } else if (action === "report") {
        await reportCmd(env, chat, user, origin, dbOps);
      } else {
        await showMenu(env, chat.id, session);
      }
      await saveSession(env, chat.id, session);
      return;
    }

    await answerCb();
  } catch (err) {
    console.error("callback_error", String(err));
  }
}

async function handleMessage(msg, env, origin, dbOps) {
  const chat = msg.chat;
  const user = msg.from;
  const uid = user?.id || 0;
  const rawText = String(msg.text || "").trim();
  if (!chat || !rawText) return;

  const session = await loadSession(env, chat.id);

  // Slash commands
  if (rawText.startsWith("/")) {
    const firstSpace = rawText.search(/\s/);
    const rawCmd = (firstSpace === -1 ? rawText : rawText.slice(0, firstSpace)).slice(1);
    const cmd = rawCmd.split("@")[0].toLowerCase();
    const argText = firstSpace === -1 ? "" : rawText.slice(firstSpace + 1).trim();
    const args = argText ? argText.split(/\s+/) : [];

    if (cmd === "start") {
      if (chat.type === "private") {
        session.mode = "";
        if (argText === "water") {
          await waterCompare(env, chat, session);
          await saveSession(env, chat.id, session);
          return;
        }
        if (argText === "alert") {
          await alertMenu(env, chat, uid, session, dbOps);
          await saveSession(env, chat.id, session);
          return;
        }
        if (argText === "compare") {
          await compareCmd(env, chat, session);
          await saveSession(env, chat.id, session);
          return;
        }
        clearCompare(session);
        if (!isAdmin(env, chat.id)) {
          await shopperMenu(env, chat.id);
          await saveSession(env, chat.id, session);
          return;
        }
      }
      if (!isAdminUpdate(env, user, chat)) {
        await replyTo(env, chat.id, "🍯 꿀템봇입니다. 개인 대화에서 /alert 로 키워드 알림, /compare 로 단가 비교를 이용해 보세요.");
        return;
      }
      await showMenu(env, chat.id, session);
      await saveSession(env, chat.id, session);
      return;
    }

    if (cmd === "menu" || cmd === "help") {
      if (chat.type === "private") {
        clearCompare(session);
        session.mode = "";
        if (!isAdmin(env, chat.id)) {
          await shopperMenu(env, chat.id);
          await saveSession(env, chat.id, session);
          return;
        }
      }
      if (!isAdminUpdate(env, user, chat)) return;
      await showMenu(env, chat.id, session);
      await saveSession(env, chat.id, session);
      return;
    }

    if (cmd === "compare") {
      await compareCmd(env, chat, session);
      await saveSession(env, chat.id, session);
      return;
    }

    if (cmd === "cancel") {
      if (chat.type === "private" && session.has_compare_session) {
        clearCompare(session);
        await replyTo(env, chat.id, "단가 비교를 취소했어요.", { reply_markup: compareResultKb() });
        await saveSession(env, chat.id, session);
      }
      return;
    }

    if (cmd === "alert" || cmd === "alerts") {
      await alertMenu(env, chat, uid, session, dbOps);
      await saveSession(env, chat.id, session);
      return;
    }

    if (cmd === "status") {
      await flyerStatus(env, chat.id, user, chat, session, dbOps);
      await saveSession(env, chat.id, session);
      return;
    }

    if (cmd === "search") {
      if (!isAdminUpdate(env, user, chat)) return;
      if (args.length === 0) await searchMenu(env, chat.id, session);
      else await searchCmd(env, chat, user, argText);
      await saveSession(env, chat.id, session);
      return;
    }

    if (cmd === "clicks" || cmd === "click") {
      await clicksCmd(env, chat, user, origin, dbOps);
      return;
    }

    if (cmd === "report" || cmd === "stats") {
      await reportCmd(env, chat, user, origin, dbOps);
      return;
    }
    if (cmd === "클릭") {
      await clicksCmd(env, chat, user, origin, dbOps);
      return;
    }
    if (cmd === "통계") {
      await reportCmd(env, chat, user, origin, dbOps);
      return;
    }
    if (cmd === "알림") {
      await alertMenu(env, chat, uid, session, dbOps);
      await saveSession(env, chat.id, session);
      return;
    }
  }

  // Plain text handling
  if (rawText === "⚖️ 단가 비교") {
    if (session.mode === "alert_wait") session.mode = "";
    await compareCmd(env, chat, session);
    await saveSession(env, chat.id, session);
    return;
  }

  const remoteBtns = new Set(["📰 전단지 현황", "⚡ 실시간 클릭", "📊 리포트", "🔍 검색", "⚖️ 단가 비교", "🔔 키워드 알림"]);
  const isRemoteBtn = isAdmin(env, uid) && remoteBtns.has(rawText);

  if (chat.type === "private") {
    if (["🔔 키워드 알림", "키워드 알림", "/알림", "알림"].includes(rawText)) {
      await alertMenu(env, chat, uid, session, dbOps);
      await saveSession(env, chat.id, session);
      return;
    }
    if (/^(?:생수|물)\s*2\s*(?:l|리터)(?:\s*(?:비교|얼마|추천))?\??$/i.test(rawText)) {
      session.mode = "";
      await waterCompare(env, chat, session);
      await saveSession(env, chat.id, session);
      return;
    }
    if (rawText === "🏠 메뉴") {
      clearCompare(session);
      session.mode = "";
      if (!isAdmin(env, chat.id)) await shopperMenu(env, chat.id);
      else await showMenu(env, chat.id, session);
      await saveSession(env, chat.id, session);
      return;
    }
    if (isRemoteBtn) {
      clearCompare(session);
      session.mode = "";
    }
    if (session.mode === "alert_wait" && !isRemoteBtn && !rawText.includes("://")) {
      const kws = rawText.split(/[,;\r\n]+/).map((k) => k.trim()).filter(Boolean).slice(0, 5);
      if (kws.length > 0) {
        const msgs = [];
        const collectedRecent = [];
        const seenCodes = new Set();
        for (const kw of kws) {
          const resAdd = await addAlertMessage(env, origin, uid, kw, dbOps);
          msgs.push(resAdd.message || "");
          for (const rm of resAdd.recent_matches || []) {
            const code = String(rm.code || "");
            if (code && !seenCodes.has(code)) {
              seenCodes.add(code);
              collectedRecent.push(rm);
            }
          }
        }
        await alertMenu(env, chat, uid, session, dbOps, {
          notice: msgs.join(" / "),
          recentMatches: collectedRecent.slice(0, 3),
        });
        await saveSession(env, chat.id, session);
        return;
      }
    }
    if (session.has_compare_session) {
      await compareInput(env, chat.id, session, rawText);
      await saveSession(env, chat.id, session);
      return;
    }
    if (
      session.mode !== "search_wait" &&
      session.mode !== "alert_wait" &&
      !rawText.includes("://") &&
      /\d\s*(?:ml|kg|g|l|리터|그램|병|개|캔|봉|팩|롤|매)/i.test(rawText)
    ) {
      await compareInput(env, chat.id, session, rawText);
      await saveSession(env, chat.id, session);
      return;
    }
    if (!isAdmin(env, uid)) {
      await shopperMenu(env, chat.id);
      return;
    }
  }

  if (!isAdminUpdate(env, user, chat)) {
    await replyTo(env, chat.id, `안녕하세요! 오늘의 핫 꿀템 알리미입니다. 🍯\n(사용자 ID: <code>${uid}</code>)`, { parse_mode: "HTML" });
    return;
  }

  if (rawText === "📰 전단지 현황") {
    session.mode = "";
    await flyerStatus(env, chat.id, user, chat, session, dbOps);
    await saveSession(env, chat.id, session);
    return;
  }
  if (["⚡ 실시간 클릭", "실시간 클릭", "클릭", "/클릭"].includes(rawText)) {
    session.mode = "";
    await clicksCmd(env, chat, user, origin, dbOps);
    await saveSession(env, chat.id, session);
    return;
  }
  if (["🔍 검색", "검색"].includes(rawText)) {
    await searchMenu(env, chat.id, session);
    await saveSession(env, chat.id, session);
    return;
  }
  if (["📊 리포트", "리포트", "통계", "/통계"].includes(rawText)) {
    session.mode = "";
    await reportCmd(env, chat, user, origin, dbOps);
    await saveSession(env, chat.id, session);
    return;
  }
  if (["🏠 메뉴", "메뉴"].includes(rawText)) {
    await showMenu(env, chat.id, session);
    await saveSession(env, chat.id, session);
    return;
  }

  if (session.mode === "search_wait") {
    session.mode = "";
    await saveSession(env, chat.id, session);
    await searchCmd(env, chat, user, rawText);
    return;
  }

  const urls = rawText.match(/https?:\/\/[^\s]+coupang\.com\/[^\s]+/g);
  if (!urls || urls.length === 0) {
    await showMenu(env, chat.id, session);
    await saveSession(env, chat.id, session);
    return;
  }

  session.mode = "";
  await saveSession(env, chat.id, session);
  const targetUrl = urls[0];
  const customNote = rawText.replace(targetUrl, "").trim() || null;
  const statusMsg = await replyTo(env, chat.id, "🔗 쿠팡 파트너스 딥링크 생성 중...");
  const sMsgId = statusMsg?.message_id;
  try {
    const res = await createDeeplink(env, [targetUrl], "tgmanual");
    const data = res?.data || [];
    if (!data.length) {
      if (sMsgId) await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: sMsgId, text: "❌ 딥링크 생성 실패 (응답 데이터 없음)" });
      return;
    }
    const deeplink = data[0].shortenUrl || data[0].landingUrl;
    const noteStr = customNote ? `\n📦 <b>입력된 수량/메모</b>: <code>${esc(customNote)}</code>` : "";
    const previewText = `✅ <b>파트너스 딥링크 변환 완료!</b>\n\n🔗 <b>단축 딥링크</b>: ${esc(deeplink)}${noteStr}`;
    const markup = { inline_keyboard: [[{ text: "👉 링크 열기", url: deeplink }]] };
    if (sMsgId) {
      await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: sMsgId, text: previewText, reply_markup: markup, parse_mode: "HTML" });
    } else {
      await replyTo(env, chat.id, previewText, { reply_markup: markup, parse_mode: "HTML" });
    }
  } catch (err) {
    if (sMsgId) await tgApi(env, "editMessageText", { chat_id: chat.id, message_id: sMsgId, text: `❌ 딥링크 생성 중 오류 발생: ${esc(err.message)}` }).catch(() => {});
  }
}

export async function handleTelegramUpdate(update, env, origin, dbOps) {
  if (!update || typeof update !== "object") return;
  if (update.chat_member) {
    await handleChatMemberUpdate(update.chat_member, env, dbOps);
    return;
  }
  if (update.callback_query) {
    await handleCallbackQuery(update.callback_query, env, origin, dbOps);
    return;
  }
  if (update.message) {
    await handleMessage(update.message, env, origin, dbOps);
  }
}
