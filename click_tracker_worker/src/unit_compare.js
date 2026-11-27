function bigAbs(n) {
  return n < 0n ? -n : n;
}

function bigGcd(a, b) {
  let x = bigAbs(a);
  let y = bigAbs(b);
  while (y !== 0n) {
    const t = x % y;
    x = y;
    y = t;
  }
  return x || 1n;
}

export function makeFrac(num, den = 1n) {
  let n = BigInt(num);
  let d = BigInt(den);
  if (d === 0n) throw new Error("0으로 나눌 수 없습니다.");
  if (d < 0n) {
    n = -n;
    d = -d;
  }
  if (n === 0n) return { num: 0n, den: 1n };
  const g = bigGcd(n, d);
  return { num: n / g, den: d / g };
}

export function fracFromDecimal(raw) {
  const s = String(raw).trim();
  if (!/^-?[0-9]+(?:\.[0-9]+)?$/.test(s)) {
    throw new Error(`올바른 숫자가 아닙니다: ${s}`);
  }
  const neg = s.startsWith("-");
  const clean = neg ? s.slice(1) : s;
  const [intPart, fracPart = ""] = clean.split(".");
  const den = 10n ** BigInt(fracPart.length);
  const num = BigInt(`${intPart}${fracPart}`);
  return makeFrac(neg ? -num : num, den);
}

export function fracMul(a, b) {
  return makeFrac(a.num * b.num, a.den * b.den);
}

export function fracDiv(a, b) {
  if (b.num === 0n) throw new Error("0으로 나눌 수 없습니다.");
  return makeFrac(a.num * b.den, a.den * b.num);
}

export function fracCmp(a, b) {
  const lhs = a.num * b.den;
  const rhs = b.num * a.den;
  if (lhs < rhs) return -1;
  if (lhs > rhs) return 1;
  return 0;
}

function addCommasBigInt(n) {
  const s = n.toString();
  return s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function formatRate(rate) {
  if (rate.den === 1n) {
    return `${addCommasBigInt(rate.num)}원`;
  }
  if (fracCmp(rate, makeFrac(1n, 100n)) < 0) {
    return "0.01원 미만";
  }
  // Exact 2-decimal round-half-even matching Python Decimal quantization
  const num = bigAbs(rate.num);
  const den = rate.den;
  const scaledNum = num * 100n;
  let q = scaledNum / den;
  const rem = scaledNum % den;
  const twiceRem = rem * 2n;
  if (twiceRem > den || (twiceRem === den && (q & 1n) === 1n)) {
    q += 1n;
  }
  const intPart = q / 100n;
  const fracPart = q % 100n;
  let fracStr = fracPart.toString().padStart(2, "0").replace(/0+$/, "");
  const sign = rate.num < 0n ? "-" : "";
  const formatted = fracStr
    ? `${sign}${addCommasBigInt(intPart)}.${fracStr}`
    : `${sign}${addCommasBigInt(intPart)}`;
  return `약 ${formatted}원`;
}

export function serializeOffer(offer) {
  if (!offer) return null;
  return {
    total: offer.total,
    unit: offer.unit,
    unit_price_num: offer.unitPrice.num.toString(),
    unit_price_den: offer.unitPrice.den.toString(),
    count: offer.count,
    shipping: offer.shipping === undefined ? null : offer.shipping,
    count_label: offer.countLabel || "개",
  };
}

export function deserializeOffer(raw) {
  if (!raw || typeof raw !== "object") return null;
  return {
    total: Number(raw.total),
    unit: String(raw.unit),
    unitPrice: makeFrac(BigInt(raw.unit_price_num), BigInt(raw.unit_price_den)),
    count: Number(raw.count),
    shipping: raw.shipping === undefined ? null : raw.shipping,
    countLabel: String(raw.count_label || "개"),
  };
}

const MONEY_RE = /^(?:[0-9]{1,10}|[0-9]{1,3}(?:,[0-9]{3}){1,2})\s*원?$/;
const PACK_RE = /^([0-9]{1,7}(?:\.[0-9]{1,3})?)\s*(kg|g|ml|l|개)(?:\s*[x×*]\s*([0-9]{1,5})\s*개?)?$/i;
const UNITS = {
  ml: ["1L", makeFrac(1000n)],
  l: ["1L", makeFrac(1n)],
  g: ["100g", makeFrac(100n)],
  kg: ["100g", makeFrac(1n, 10n)],
  개: ["1개", makeFrac(1n)],
};

function won(text) {
  if (!MONEY_RE.test(text)) {
    throw new Error("금액은 9,900원처럼 원 단위 숫자로 입력해 주세요.");
  }
  const stripped = text.endsWith("원") ? text.slice(0, -1) : text;
  return Number.parseInt(stripped.replaceAll(",", "").trim(), 10);
}

export function parseOffer(text) {
  const parts = String(text || "").split("/").map((p) => p.trim());
  if (parts.length !== 3 || !parts.every(Boolean)) {
    throw new Error("상품값 / 구성 / 배송비를 모두 입력해 주세요. 무료배송이면 마지막에 0원을 적어 주세요.");
  }
  const price = won(parts[0]);
  const fee = parts[2] === "무료배송" ? 0 : won(parts[2]);
  const pack = PACK_RE.exec(parts[1]);
  if (!pack) {
    throw new Error("구성은 500ml×40, 1.5kg, 24개처럼 선택한 옵션 하나만 적어 주세요.");
  }
  const amount = fracFromDecimal(pack[1]);
  const count = Number.parseInt(pack[3] || "1", 10);
  if (price <= 0 || amount.num <= 0n || count <= 0) {
    throw new Error("상품값, 용량, 수량은 모두 0보다 커야 합니다.");
  }
  const [unit, scale] = UNITS[pack[2].toLowerCase()];
  if (unit === "1개" && amount.den !== 1n) {
    throw new Error("개수는 정수로 입력해 주세요.");
  }
  const total = price + fee;
  const totalFrac = makeFrac(BigInt(total));
  const denom = fracMul(amount, makeFrac(BigInt(count)));
  const unitPrice = fracDiv(fracMul(totalFrac, scale), denom);
  const countVal = unit === "1개" ? Number(amount.num) * count : count;
  return {
    total,
    unit,
    unitPrice,
    count: countVal,
    shipping: fee,
    countLabel: "개",
  };
}

const SIZE_PART = "(?<size>[0-9]{1,7}(?:\\.[0-9]{1,3})?)\\s*(?<unit>밀리리터|리터|킬로그램|그램|ml|kg|g|L)";
const NATURAL_PACK_SRC = `(?:${SIZE_PART}\\s*,?\\s*(?:[x×*]\\s*)?)?(?<count>[0-9]{1,5})\\s*(?<label>개입|개|병|캔|봉|팩|롤|매)`;
const WON_NUMBER = "(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)";
const SHIP_FEE_RE = new RegExp(`(?:배송비|택배비|배송료|유료배송)\\s*[:+]?\\s*(${WON_NUMBER})\\s*원?`, "g");
const FREE_RE = /무료\s*배송|배송비\s*무료|무배/;
const UNCLEAR_RE = /부터|이상|이하|택\s*1|또는|중\s*택|옵션별|적립|체감가|정상가|아님|아닙|제외|조건부|회원|한정|최대|[~～]/;
const UNIT_NAMES = { 밀리리터: "ml", 리터: "l", 킬로그램: "kg", 그램: "g" };

export function readPack(rawText) {
  let text = String(rawText || "").normalize("NFKC");
  text = text.replace(/(?<=[0-9]),(?=[0-9]{3}(?:[^0-9]|$))/g, "");
  text = text.replace(/([x×*]\s*[0-9]+)(?=\s*(?:$|[),]))/gi, "$1개");
  const packRe = new RegExp(NATURAL_PACK_SRC, "gi");
  const matches = Array.from(text.matchAll(packRe));
  if (matches.length !== 1 || UNCLEAR_RE.test(text)) {
    throw new Error("선택한 상품 한 가지의 용량과 수량을 적어 주세요. 예) 생수 2L 6병");
  }
  const m = matches[0];
  const outside = `${text.slice(0, m.index)} ${text.slice(m.index + m[0].length)}`;
  if (/[0-9]|[×*+]|(?:^|\s)-\s*$/.test(outside)) {
    throw new Error("묶음 구성이 여러 가지로 읽혀요. 실제 구매할 구성 하나만 보내 주세요.");
  }
  const groups = m.groups || {};
  const count = Number.parseInt(groups.count, 10);
  const unitRaw = String(groups.unit || "개").toLowerCase();
  return [groups.size || "1", UNIT_NAMES[unitRaw] || unitRaw, count, groups.label];
}

export function naturalOffer(rawText) {
  let text = String(rawText || "").normalize("NFKC").trim();
  if (UNCLEAR_RE.test(text) || /(?:개|병|팩|봉|롤|캔|매|리터|ml|l|kg|g)\s*당/i.test(text)) {
    throw new Error("선택한 구성과 실제 상품값 하나를 보내 주세요.");
  }
  const feeRe = new RegExp(SHIP_FEE_RE.source, "g");
  const fees = Array.from(text.matchAll(feeRe));
  const free = FREE_RE.test(text);
  if (fees.length > 1 || (fees.length > 0 && free)) {
    throw new Error("이 묶음에 적용되는 배송비 하나만 적어 주세요.");
  }
  let fee = null;
  if (fees.length > 0) {
    fee = won(fees[0][1]);
    text = text.replace(feeRe, " ");
  } else if (free) {
    fee = 0;
    text = text.replace(FREE_RE, " ");
  }
  if (/배송비|택배비|배송료|착불|유료배송/.test(text)) {
    throw new Error("배송비는 ‘배송비 3,000원’처럼 적거나 생략해 주세요.");
  }
  const wonPriceRe = new RegExp(`(?<![0-9.,-])(${WON_NUMBER})\\s*원`, "g");
  let prices = Array.from(text.matchAll(wonPriceRe));
  if (prices.length === 0) {
    const tailPriceRe = new RegExp(`(?<=\\s)(${WON_NUMBER})\\s*$`, "g");
    prices = Array.from(text.matchAll(tailPriceRe));
  }
  if (prices.length !== 1) {
    throw new Error("상품값 하나를 적어 주세요. 예) 생수 2L 6병 5,400원");
  }
  const priceMatch = prices[0];
  const rest = `${text.slice(0, priceMatch.index)} ${text.slice(priceMatch.index + priceMatch[0].length)}`;
  const [size, unit, count, label] = readPack(rest);
  const parsed = parseOffer(`${priceMatch[1]} / ${size}${unit}×${count} / ${fee || 0}`);
  return {
    total: parsed.total,
    unit: parsed.unit,
    unitPrice: parsed.unitPrice,
    count,
    shipping: fee,
    countLabel: label,
  };
}

export function describeOffer(offer) {
  const basis = offer.shipping === null || offer.shipping === undefined
    ? "상품값 기준 · 배송비 미확인"
    : "배송비 포함";
  const lines = [`${offer.count}${offer.countLabel} · ${offer.total.toLocaleString("en-US")}원 (${basis})`];
  if (offer.unit !== "1개") {
    lines.push(`1${offer.countLabel}당 ${formatRate(makeFrac(BigInt(offer.total), BigInt(offer.count)))}`);
  }
  lines.push(`${offer.unit}당 ${formatRate(offer.unitPrice)}`);
  return lines.join("\n");
}

export function compareOffers(first, second) {
  if (first.unit !== second.unit) {
    throw new Error("두 상품을 같은 기준으로 입력해 주세요. 무게끼리, 용량끼리, 개수끼리 비교할 수 있어요.");
  }
  const goodsOnly = first.shipping === null || first.shipping === undefined ||
    second.shipping === null || second.shipping === undefined;
  const lines = [goodsOnly ? "🍯 상품값 기준 단가 · 배송비 제외" : "🍯 배송비 포함 단가"];
  const rates = [];
  for (const [name, offer] of [["첫째", first], ["둘째", second]]) {
    let rate = offer.unitPrice;
    let total = offer.total;
    if (goodsOnly) {
      total -= offer.shipping || 0;
      rate = fracMul(rate, makeFrac(BigInt(total), BigInt(offer.total)));
    }
    rates.push(rate);
    lines.push(`${name} · 총 ${total.toLocaleString("en-US")}원 · ${offer.unit}당 ${formatRate(rate)}`);
  }
  const cmp = fracCmp(rates[0], rates[1]);
  let result;
  if (cmp === 0) {
    result = "두 상품의 단가가 같아요.";
  } else {
    const cheaper = cmp < 0 ? "첫째" : "둘째";
    result = `${cheaper} 상품의 ${first.unit}당 가격이 더 낮아요.`;
  }
  return [...lines, "", result].join("\n");
}

export function waterRankings(rows) {
  const products = [];
  const seen = new Set();
  for (const row of rows || []) {
    if (!row || typeof row !== "object") continue;
    const name = String(row.productName || "");
    if (row.categoryName !== "식품" || !/생수|샘물|삼다수|석수|알카리수|광천수|미네랄워터/.test(name)) {
      continue;
    }
    if (/공병|생수병|탄산|주스|보리차/.test(name)) continue;
    try {
      const [size, unit, count, label] = readPack(name);
      const rawPriceNum = Number(row.productPrice);
      const url = String(row.productUrl || "");
      let parsedUrl;
      try {
        parsedUrl = new URL(url);
      } catch {
        continue;
      }
      if (parsedUrl.protocol !== "https:" || !parsedUrl.hostname.toLowerCase().endsWith(".coupang.com")) {
        continue;
      }
      const sizeFrac = fracFromDecimal(size);
      const is2L = (unit === "l" && sizeFrac.num === 2n && sizeFrac.den === 1n) ||
        (unit === "ml" && sizeFrac.num === 2000n && sizeFrac.den === 1n);
      if (!is2L) continue;
      if (!["개", "병", "개입"].includes(label) || count < 1 || !Number.isInteger(rawPriceNum) || rawPriceNum <= 0) {
        continue;
      }
      const key = `${row.productId || name}|${count}|${rawPriceNum}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const rate = makeFrac(BigInt(rawPriceNum), BigInt(2 * count));
      products.push([rate, name, count, rawPriceNum, row.isFreeShipping === true, url]);
    } catch {
      continue;
    }
  }
  products.sort((a, b) => fracCmp(a[0], b[0]));
  return products.slice(0, 3);
}

// ComparePad (stateless JSON-serializable port of compare_pad.py)
function randomTokenHex4() {
  const buf = new Uint8Array(4);
  crypto.getRandomValues(buf);
  return Array.from(buf).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function createPad() {
  return {
    values: {},
    digits: "",
    typing: false,
    token: randomTokenHex4(),
  };
}

export function padSteps(pad) {
  return pad?.values?.unit === "개"
    ? ["unit", "count", "price", "fee"]
    : ["unit", "amount", "count", "price", "fee"];
}

export function padFieldName(pad) {
  const vals = pad?.values || {};
  return padSteps(pad).find((step) => !(step in vals));
}

export function padChoices(pad, first) {
  const step = padFieldName(pad);
  const unit = pad?.values?.unit;
  if (step === "unit") {
    const unitMap = { "1L": ["ml", "L"], "100g": ["g", "kg"], "1개": ["개"] };
    const list = first ? unitMap[first.unit] : ["ml", "L", "g", "kg", "개"];
    return list.map((u) => [u, u]);
  }
  if (step === "amount") {
    const opts = unit === "ml" || unit === "g" ? [250, 500, 750, 1000] : [0.5, 1, 1.5, 2];
    return opts.map((v) => [`${v}${unit}`, String(v)]);
  }
  if (step === "count") {
    return [1, 2, 6, 12, 20, 24, 30, 40].map((v) => [`${v}개`, String(v)]);
  }
  const opts = step === "price" ? [4900, 9900, 12000, 12900, 19900] : [0, 2500, 3000, 3500, 4000];
  return opts.map((v) => [v === 0 ? "무료배송" : `${v.toLocaleString("en-US")}원`, String(v)]);
}

export function padScreen(pad, first) {
  const step = padFieldName(pad);
  const questions = {
    unit: "어떤 단위로 비교할까요?",
    amount: "한 개의 용량이나 무게를 고르세요.",
    count: "묶음 전체에 몇 개가 들어 있나요?",
    price: "배송비를 뺀 묶음 전체 상품값을 고르세요.",
    fee: "이 묶음에 실제로 붙는 배송비를 고르세요.",
  };
  let text = `${first ? "둘째" : "첫째"} 상품 · ${questions[step]}`;
  const btn = (label, action) => ({ text: label, callback_data: `pad:${pad.token}:${action}` });
  let rows = [];
  if (pad.typing) {
    text += `\n\n입력한 값  ${pad.digits || "＿"}`;
    rows = ["123", "456", "789"].map((r) => [...r].map((n) => btn(n, `d${n}`)));
    const last = [btn("0", "d0"), btn("⌫", "del")];
    if (step === "amount") last.unshift(btn(".", "d."));
    rows.push(last, [btn("✅ 입력 완료", "ok")]);
  } else {
    const btns = padChoices(pad, first).map(([label, val]) => btn(label, `v${val}`));
    for (let i = 0; i < btns.length; i += 3) {
      rows.push(btns.slice(i, i + 3));
    }
    if (step !== "unit") rows.push([btn("🔢 다른 값", "custom")]);
  }
  rows.push([btn("↩ 이전", "back"), { text: "🏠 메뉴", callback_data: "compare:home" }]);
  return [text, { inline_keyboard: rows }];
}

export function padApply(pad, action, first) {
  const step = padFieldName(pad);
  if (action === "back") {
    const keys = Object.keys(pad.values);
    if (!pad.typing && keys.length > 0) {
      delete pad.values[keys[keys.length - 1]];
    }
    pad.typing = false;
    pad.digits = "";
  } else if (action === "custom" && step !== "unit") {
    pad.typing = true;
  } else if (pad.typing && action.startsWith("d") && action !== "del") {
    const digit = action.slice(1);
    if (!"0123456789.".includes(digit) || digit.length !== 1 || (digit === "." && (step !== "amount" || pad.digits.includes(".")))) {
      throw new Error("입력할 수 없는 기호예요.");
    }
    const limit = { amount: 11, count: 5, fee: 10, price: 10 }[step];
    if (pad.digits.length >= limit) {
      throw new Error("입력할 수 있는 자릿수를 넘었어요.");
    }
    pad.digits += digit;
    return null;
  } else if (pad.typing && action === "del") {
    pad.digits = pad.digits.slice(0, -1);
    return null;
  } else {
    let value;
    const validChoices = Object.fromEntries(padChoices(pad, first).map(([lbl, v]) => [v, lbl]));
    if (pad.typing && action === "ok") {
      value = pad.digits;
      const pattern = step === "amount" ? /^[0-9]{1,7}(?:\.[0-9]{1,3})?$/ : /^[0-9]+$/;
      if (!pattern.test(value) || (step !== "fee" && Number.parseFloat(value) === 0)) {
        throw new Error("값을 확인해 주세요. 무료배송은 배송비에서 0을 선택하세요.");
      }
    } else if (!pad.typing && action.startsWith("v") && action.slice(1) in validChoices) {
      value = action.slice(1);
    } else {
      throw new Error("현재 화면의 버튼을 눌러 주세요.");
    }
    pad.values[step] = value;
    pad.typing = false;
    pad.digits = "";
  }
  pad.token = randomTokenHex4();
  if ("fee" in pad.values) {
    const v = pad.values;
    const pack = v.unit === "개" ? `${v.count}개` : `${v.amount}${v.unit}×${v.count}`;
    return parseOffer(`${v.price} / ${pack} / ${v.fee}`);
  }
  return null;
}
