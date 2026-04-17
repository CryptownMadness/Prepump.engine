import requests
import time
from datetime import datetime, timezone

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = "8594524303:AAHHVLLiTvzwFtRctwQ4DfzJIKKefvxb1yI"
CHAT_ID = "6072138414"
CHECK_INTERVAL = 180  # every 3 minutes
MINIMUM_SCORE = 4     # only alert if score >= 4
seen_pairs = set()

# ============================================================
# TELEGRAM SENDER
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ============================================================
# FETCH NEW BASE PAIRS FROM DEXSCREENER
# ============================================================
def fetch_new_pairs():
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    try:
        res = requests.get(url, timeout=15)
        data = res.json()
        base_pairs = [t for t in data if t.get("chainId") == "base"]
        return base_pairs
    except Exception as e:
        print(f"Fetch error: {e}")
        return []

# ============================================================
# FETCH PAIR DETAILS
# ============================================================
def fetch_pair_details(token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = requests.get(url, timeout=15)
        data = res.json()
        pairs = data.get("pairs", [])
        base_pairs = [p for p in pairs if p.get("chainId") == "base"]
        if base_pairs:
            return max(base_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
        return None
    except Exception as e:
        print(f"Pair detail error: {e}")
        return None

# ============================================================
# SCORING ENGINE
# ============================================================
def score_token(pair):
    score = 0
    reasons = []

    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    fdv = float(pair.get("fdv", 0) or 0)
    volume_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
    volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
    txns_1h_buys = int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
    txns_1h_sells = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
    txns_1h_total = txns_1h_buys + txns_1h_sells
    price_change_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)
    price_change_5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)

    # Pair age in hours
    pair_created_at = pair.get("pairCreatedAt")
    age_hours = 999
    if pair_created_at:
        created = datetime.fromtimestamp(pair_created_at / 1000, tz=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        age_hours = (now - created).total_seconds() / 3600

    # ---- SCORING RULES ----

    # Liquidity check
    if 15000 <= liquidity <= 100000:
        score += 1
        reasons.append("✅ Liquidity in degen range")
    elif liquidity > 100000:
        score += 2
        reasons.append("✅ Strong liquidity")
    elif liquidity < 10000:
        score -= 1
        reasons.append("⚠️ Low liquidity")

    # FDV check
    if fdv >= 150000:
        score += 1
        reasons.append("✅ FDV above $150k")
    else:
        reasons.append("⚠️ FDV below $150k")

    # Age check
    if age_hours <= 2:
        score += 2
        reasons.append("🔥 Very new (under 2 hrs)")
    elif age_hours <= 24:
        score += 1
        reasons.append("✅ New pair (under 24 hrs)")
    elif age_hours > 60:
        score -= 1
        reasons.append("⚠️ Older pair")

    # Transaction activity
    if txns_1h_total >= 150:
        score += 2
        reasons.append("🔥 High txn activity (150+/hr)")
    elif txns_1h_total >= 50:
        score += 1
        reasons.append("✅ Good txn activity (50+/hr)")
    else:
        reasons.append("⚠️ Low txn activity")

    # Buy pressure
    if txns_1h_total > 0:
        buy_ratio = txns_1h_buys / txns_1h_total
        if buy_ratio >= 0.60:
            score += 2
            reasons.append(f"🔥 Strong buy pressure ({int(buy_ratio*100)}% buys)")
        elif buy_ratio >= 0.50:
            score += 1
            reasons.append(f"✅ Positive buy pressure ({int(buy_ratio*100)}% buys)")
        else:
            reasons.append(f"⚠️ Sell pressure ({int(buy_ratio*100)}% buys)")

    # Price momentum
    if price_change_5m > 10:
        score += 2
        reasons.append(f"🚀 5m price +{price_change_5m}%")
    elif price_change_5m > 0:
        score += 1
        reasons.append(f"✅ 5m price positive (+{price_change_5m}%)")

    if price_change_1h > 20:
        score += 1
        reasons.append(f"📈 1h price +{price_change_1h}%")

    # Volume momentum
    if volume_1h > 50000:
        score += 1
        reasons.append(f"✅ 1H volume ${int(volume_1h):,}")

    return score, reasons, age_hours

# ============================================================
# FORMAT ALERT MESSAGE
# ============================================================
def format_alert(pair, score, reasons, age_hours):
    name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    ca = pair.get("baseToken", {}).get("address", "")
    price = pair.get("priceUsd", "N/A")
    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    fdv = float(pair.get("fdv", 0) or 0)
    volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
    txns_1h_buys = int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
    txns_1h_sells = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
    price_change_5m = pair.get("priceChange", {}).get("m5", "N/A")
    price_change_1h = pair.get("priceChange", {}).get("h1", "N/A")
    dex_url = pair.get("url", f"https://dexscreener.com/base/{ca}")

    # Score label
    if score >= 8:
        label = "🔴 STRONG SIGNAL"
    elif score >= 6:
        label = "🟠 GOOD SIGNAL"
    elif score >= 4:
        label = "🟡 WATCH SIGNAL"
    else:
        label = "⚪ WEAK"

    age_str = f"{age_hours:.1f} hrs" if age_hours < 24 else f"{age_hours/24:.1f} days"

    msg = f"""
{label} — Score: {score}/12

🪙 <b>{name} (${symbol})</b>
💰 Price: ${price}
🏊 Liquidity: ${int(liquidity):,}
📊 FDV: ${int(fdv):,}
📈 Volume 24H: ${int(volume_24h):,}
⏱ Age: {age_str}
🟢 Buys/Sells (1H): {txns_1h_buys} / {txns_1h_sells}
⚡ 5M Change: {price_change_5m}%
📉 1H Change: {price_change_1h}%

📋 CA:
<code>{ca}</code>

🔍 Reasons:
{chr(10).join(reasons)}

🔗 <a href="{dex_url}">Open on DexScreener</a>
🔗 <a href="https://basescan.org/token/{ca}">BaseScan</a>
"""
    return msg.strip()

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    print("🚀 Base Sniper Bot Started")
    send_telegram("🚀 <b>Base Sniper Bot is LIVE</b>\n\nMonitoring Base chain every 3 minutes.\nMinimum score to alert: 4/12\n\nWaiting for gems... 👀")

    while True:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning Base chain...")
        new_tokens = fetch_new_pairs()
        print(f"Found {len(new_tokens)} tokens on Base")

        for token in new_tokens:
            token_address = token.get("tokenAddress", "")
            if not token_address or token_address in seen_pairs:
                continue

            seen_pairs.add(token_address)
            pair = fetch_pair_details(token_address)

            if not pair:
                continue

            score, reasons, age_hours = score_token(pair)
            print(f"Token: {pair.get('baseToken', {}).get('symbol', '?')} | Score: {score} | Age: {age_hours:.1f}h")

            if score >= MINIMUM_SCORE:
                msg = format_alert(pair, score, reasons, age_hours)
                send_telegram(msg)
                print(f"✅ Alert sent for {pair.get('baseToken', {}).get('symbol', '?')}")

            time.sleep(1)  # avoid rate limiting

        print(f"Sleeping {CHECK_INTERVAL} seconds...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
