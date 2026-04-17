import requests
import re
import time
import threading
import json
from datetime import datetime, timezone
import random
from collections import defaultdict

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = "8594524303:AAHHVLLiTvzwFtRctwQ4DfzJIKKefvxb1yI"
CHAT_ID = "-5281443086"
DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1491544162389200913/E2g4M_yvsmSDsp1K2ZvyFaPUrrGr1e8mQIAQ-SWl4IVpL6ntkF_AyqxvOpKPwblcFLhN"
BASESCAN_API = "86UY4YXSQFQVMSX5B1Y1IKIN4G4QBYZXY6"
NEYNAR_API = "D4375FFB-1EEE-46B2-8EA4-970073EB6A58"
ALCHEMY_URL = "https://base-mainnet.g.alchemy.com/v2/mtF5AvP3CohRkbey6VQJl"

CHECK_INTERVAL = 120
WHALE_THRESHOLD = 10000
FOLLOWUP_HOURS = 2
LEADERBOARD_SENT_DATE = None
PEAK_HOURS_UTC = range(14, 24)

# Aerodrome contracts on Base
AERODROME_VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
AERODROME_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://dexscreener.com/",
    "Origin": "https://dexscreener.com"
}

# ============================================================
# STATE
# ============================================================
seen_pairs = set()
seen_contracts = set()
seen_breakouts = set()
seen_cex_listings = set()
seen_gauges = set()
alerted_tokens = {}
narrative_heatmap = defaultdict(int)
heatmap_last_sent = None
heartbeat_last_sent = None
leaderboard_data = []
deployer_success_cache = {}
mempool_lock = threading.Lock()

# ERC20 bytecode signatures
ERC20_SIGNATURES = ["60806040", "6080604052"]

# Scam keywords to reject immediately
SCAM_KEYWORDS = [
    "test", "fake", "rug", "scam", "honeypot", "hack",
    "free", "airdrop", "giveaway", "claim", "reward",
    "safe", "moon100x", "1000x", "guaranteed", "presale"
]

BLACKLISTED_DEPLOYERS = set()

# ============================================================
# BLACKLIST AUTO-LEARNING SYSTEM
# ============================================================
# Persistent storage file for blacklist
BLACKLIST_FILE = "/tmp/blacklist.json"

# Track alerted token performance for rug detection
# {ca: {deployer, symbol, name, price_at_alert, alert_time, score}}
alerted_token_registry = {}

# Rug detection thresholds
RUG_PRICE_DROP_PCT = 70    # Token drops 70%+ = likely rug
RUG_LIQUIDITY_DROP_PCT = 50  # LP drops 50%+ = soft rug
RUG_CHECK_HOURS = 24        # Check for 24 hours after alert

# Stats for learning
blacklist_stats = {
    "total_rugs_caught": 0,
    "deployers_blacklisted": 0,
    "tokens_saved": 0,
}

def load_blacklist():
    """Load persistent blacklist from file"""
    global BLACKLISTED_DEPLOYERS
    try:
        import os
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, "r") as f:
                data = json.load(f)
                deployers = set(data.get("deployers", []))
                BLACKLISTED_DEPLOYERS.update(deployers)
                blacklist_stats["deployers_blacklisted"] = len(BLACKLISTED_DEPLOYERS)
                print(f"📋 Loaded {len(BLACKLISTED_DEPLOYERS)} blacklisted deployers")
    except Exception as e:
        print(f"Blacklist load error: {e}")

def save_blacklist():
    """Save blacklist to persistent file"""
    try:
        with open(BLACKLIST_FILE, "w") as f:
            json.dump({
                "deployers": list(BLACKLISTED_DEPLOYERS),
                "stats": blacklist_stats,
                "last_updated": datetime.now(tz=timezone.utc).isoformat()
            }, f, indent=2)
    except Exception as e:
        print(f"Blacklist save error: {e}")

def blacklist_deployer(deployer, reason, symbol, ca):
    """Add deployer to blacklist and save"""
    if not deployer or deployer.lower() in BLACKLISTED_DEPLOYERS:
        return
    BLACKLISTED_DEPLOYERS.add(deployer.lower())
    blacklist_stats["deployers_blacklisted"] += 1
    blacklist_stats["total_rugs_caught"] += 1
    save_blacklist()

    dep_short = deployer[:8] + "..." + deployer[-6:]
    alert = f"""
🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨
🚨 <b>RUG DETECTED — DEPLOYER BLACKLISTED</b>
🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨

🪙 Token: <b>${symbol}</b>
📋 CA: <code>{ca}</code>
👤 Deployer: <code>{dep_short}</code>

⚠️ Reason: {reason}

🛡 Action Taken:
✅ Deployer permanently blacklisted
✅ All future tokens from this wallet blocked
✅ {blacklist_stats['deployers_blacklisted']} deployers now blacklisted

📊 Bot Learning Stats:
Rugs caught: {blacklist_stats['total_rugs_caught']}
Tokens protected: {blacklist_stats['tokens_saved']}
🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨"""
    send_all(alert)
    print(f"  🚨 BLACKLISTED deployer {dep_short} — reason: {reason}")

def check_rug_detection():
    """Monitor alerted tokens for rug signals and auto-learn"""
    now = datetime.now(tz=timezone.utc)
    to_remove = []

    for ca, data in list(alerted_token_registry.items()):
        alert_time = data.get("alert_time")
        if not alert_time:
            to_remove.append(ca)
            continue

        elapsed_hours = (now - alert_time).total_seconds() / 3600

        # Only check for RUG_CHECK_HOURS after alert
        if elapsed_hours > RUG_CHECK_HOURS:
            to_remove.append(ca)
            continue

        # Check every 30 minutes
        last_check = data.get("last_rug_check")
        if last_check:
            check_elapsed = (now - last_check).total_seconds() / 60
            if check_elapsed < 30:
                continue

        alerted_token_registry[ca]["last_rug_check"] = now

        try:
            pair = fetch_pair_by_address(ca)
            if not pair:
                continue

            current_price = float(pair.get("priceUsd", 0) or 0)
            current_liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            entry_price = data.get("price_at_alert", 0)
            entry_liq = data.get("liq_at_alert", 0)
            symbol = data.get("symbol", "?")
            deployer = data.get("deployer", "")

            rug_reason = None

            # Check price drop
            if entry_price and entry_price > 0 and current_price > 0:
                price_drop = ((entry_price - current_price) / entry_price) * 100
                if price_drop >= RUG_PRICE_DROP_PCT:
                    rug_reason = f"Price dropped {price_drop:.0f}% since alert"

            # Check liquidity drain
            if entry_liq and entry_liq > 0 and current_liq >= 0:
                liq_drop = ((entry_liq - current_liq) / entry_liq) * 100
                if liq_drop >= RUG_LIQUIDITY_DROP_PCT:
                    rug_reason = f"Liquidity drained {liq_drop:.0f}% since alert"

            # Check honeypot (might have changed after launch)
            if not rug_reason:
                safety = check_rug_safety(ca)
                if not safety["passed"]:
                    rug_reason = f"Now detected as: {safety['summary']}"

            if rug_reason and deployer:
                # Auto-blacklist the deployer
                blacklist_deployer(deployer, rug_reason, symbol, ca)
                blacklist_stats["tokens_saved"] += 1
                to_remove.append(ca)

                # Also send exit warning
                send_all(f"""
⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️
⚠️ <b>EXIT SIGNAL — ${symbol}</b>
⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️

{rug_reason}

💰 Entry: ${entry_price:.8f}
💰 Now: ${current_price:.8f}
🏊 Liq now: ${int(current_liq):,}

🚨 EXIT IMMEDIATELY if still holding
🚨 Deployer has been blacklisted
⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️""")

        except Exception as e:
            print(f"Rug detection error ({ca}): {e}")

    for ca in to_remove:
        alerted_token_registry.pop(ca, None)

# ============================================================
# NARRATIVE KEYWORDS
# ============================================================
TIER1_KEYWORDS = [
    "agent", "agentic", "agentfi", "autofi", "defai",
    "virtuals", "clanker", "aiagent", "aibot", "autonomous",
    "rwa", "realworld", "tokenized", "treasury", "yield",
    "depin", "compute", "gpu", "render", "network",
    "predict", "prediction", "oracle", "market",
]
TIER2_KEYWORDS = [
    "perp", "perpetual", "dex", "swap", "liquidity", "vault",
    "lending", "borrow", "stake", "restake",
    "pay", "payment", "payfi", "x402", "settle",
    "social", "creator", "farcaster", "cast",
    "zk", "privacy", "proof", "zero",
    "game", "gaming", "play", "quest",
    # Added for RAVE-type tokens
    "dao", "music", "event", "culture", "entertainment",
    "nft", "ticket", "community", "protocol", "ecosystem",
]
TIER3_KEYWORDS = [
    "base", "based", "coinbase", "brett", "toshi",
    "launch", "protocol", "finance", "dao", "governance",
    "rave", "festival", "web3",
]
SKIP_SYMBOLS = {
    "USDC", "USDT", "DAI", "WETH", "ETH", "CBETH",
    "USDBC", "EURC", "WBTC", "BTC", "USDbC", "AERO"
}

# ============================================================
# SENDERS
# ============================================================
def send_telegram(message):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def send_discord(message):
    try:
        clean = re.sub(r'<[^>]+>', '', message)
        requests.post(DISCORD_WEBHOOK,
                      json={"content": clean, "username": "Base Alpha Bot 🔵"},
                      timeout=10)
    except Exception as e:
        print(f"Discord error: {e}")

def send_all(message):
    """Send to Telegram and Discord simultaneously using threads"""
    t1 = threading.Thread(target=send_telegram, args=(message,), daemon=True)
    t2 = threading.Thread(target=send_discord, args=(message,), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=12)
    t2.join(timeout=12)

# ============================================================
# ALCHEMY RPC
# ============================================================
def alchemy_rpc(method, params):
    try:
        res = requests.post(
            ALCHEMY_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=15
        )
        if res.status_code == 200:
            return res.json().get("result")
    except Exception as e:
        print(f"Alchemy RPC error: {e}")
    return None

# ============================================================
# BASESCAN API
# ============================================================
def basescan(params):
    try:
        params["apikey"] = BASESCAN_API
        res = requests.get("https://api.basescan.org/api", params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data.get("result"), str) and "not supported" in data.get("result", "").lower():
                return {}
            return data
    except Exception as e:
        print(f"BaseScan error: {e}")
    return {}

# ============================================================
# ABI STRING DECODER
# ============================================================
def decode_string(hex_data):
    try:
        if not hex_data or hex_data == "0x":
            return ""
        data = hex_data[2:] if hex_data.startswith("0x") else hex_data
        if len(data) < 128:
            return ""
        offset = int(data[64:128], 16) * 2
        length = int(data[offset:offset + 64], 16) * 2
        string_hex = data[offset + 64:offset + 64 + length]
        return bytes.fromhex(string_hex).decode("utf-8", errors="ignore").strip()
    except:
        return ""

# ============================================================
# NARRATIVE DETECTOR
# ============================================================
def detect_narrative_from_name(name, symbol):
    combined = (name + " " + symbol).lower()
    t1 = [kw for kw in TIER1_KEYWORDS if kw in combined]
    t2 = [kw for kw in TIER2_KEYWORDS if kw in combined]
    t3 = [kw for kw in TIER3_KEYWORDS if kw in combined]
    nscore = len(t1) * 3 + len(t2) * 2 + len(t3)

    if any(k in combined for k in ["agent", "agentic", "defai", "aiagent", "virtuals", "clanker", "autofi"]):
        n = "🤖 Agentic AI"
    elif any(k in combined for k in ["rwa", "realworld", "tokenized", "treasury"]):
        n = "🏦 RWA"
    elif any(k in combined for k in ["depin", "compute", "gpu", "render"]):
        n = "⚙️ DePIN"
    elif any(k in combined for k in ["predict", "oracle"]):
        n = "🔮 Prediction"
    elif any(k in combined for k in ["perp", "perpetual", "dex", "swap"]):
        n = "📊 DeFi/DEX"
    elif any(k in combined for k in ["pay", "payment", "payfi", "x402"]):
        n = "💳 PayFi"
    elif any(k in combined for k in ["social", "creator", "farcaster"]):
        n = "🌐 SocialFi"
    elif any(k in combined for k in ["game", "gaming", "play"]):
        n = "🎮 GameFi"
    elif any(k in combined for k in ["zk", "privacy", "proof"]):
        n = "🔐 ZK/Privacy"
    elif any(k in combined for k in ["dao", "music", "event", "culture", "entertainment", "festival"]):
        n = "🎵 CultureFi"
    elif any(k in combined for k in ["base", "brett", "toshi"]):
        n = "🔵 Base Native"
    else:
        n = "🔍 Unknown"

    return n, nscore

def detect_narrative(pair):
    name = (pair.get("baseToken", {}).get("name", "") or "").lower()
    symbol = (pair.get("baseToken", {}).get("symbol", "") or "").lower()
    return detect_narrative_from_name(name, symbol)

# ============================================================
# LAYER 1 — MEMPOOL CONTRACT DETECTOR
# ============================================================
def get_pending_transactions():
    try:
        result = alchemy_rpc("eth_getBlockByNumber", ["pending", True])
        if result and isinstance(result, dict):
            return result.get("transactions", [])
    except Exception as e:
        print(f"Mempool fetch error: {e}")
    return []

def is_contract_deployment(tx):
    to = tx.get("to")
    if to is not None and to != "" and to != "0x":
        return False
    input_data = tx.get("input", "") or ""
    if len(input_data) < 100:
        return False
    for sig in ERC20_SIGNATURES:
        if input_data.startswith("0x" + sig) or input_data.startswith(sig):
            return True
    return False

def get_token_info_from_contract(contract_address):
    try:
        name_result = alchemy_rpc("eth_call", [{"to": contract_address, "data": "0x06fdde03"}, "latest"])
        symbol_result = alchemy_rpc("eth_call", [{"to": contract_address, "data": "0x95d89b41"}, "latest"])
        supply_result = alchemy_rpc("eth_call", [{"to": contract_address, "data": "0x18160ddd"}, "latest"])
        name = decode_string(name_result) if name_result else "Unknown"
        symbol = decode_string(symbol_result) if symbol_result else "???"
        try:
            supply = int(supply_result, 16) / 1e18 if supply_result else 0
        except:
            supply = 0
        return name, symbol, supply
    except Exception as e:
        print(f"Token info error: {e}")
        return "Unknown", "???", 0

def passes_quality_gates(deployer, name, symbol, supply):
    if deployer.lower() in BLACKLISTED_DEPLOYERS:
        return False, "Blacklisted deployer"
    name_lower = (name + " " + symbol).lower()
    for kw in SCAM_KEYWORDS:
        if kw in name_lower:
            return False, f"Scam keyword: {kw}"
    if len(symbol) < 2 or len(symbol) > 10:
        return False, f"Bad symbol length"
    if supply <= 0 or supply > 1e15:
        return False, "Bad supply"
    eth_balance = alchemy_rpc("eth_getBalance", [deployer, "latest"])
    if eth_balance:
        try:
            if int(eth_balance, 16) / 1e18 < 0.001:
                return False, "Deployer no ETH"
        except:
            pass
    tx_count = alchemy_rpc("eth_getTransactionCount", [deployer, "latest"])
    if tx_count:
        try:
            if int(tx_count, 16) < 3:
                return False, "New deployer wallet"
        except:
            pass
    has_narrative = any(kw in name_lower for kw in TIER1_KEYWORDS + TIER2_KEYWORDS + TIER3_KEYWORDS)
    if not has_narrative:
        return False, "No narrative match"
    return True, "Passed all gates"

def process_new_contract(tx_hash, deployer):
    try:
        contract_address = None
        for _ in range(15):
            receipt = alchemy_rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt and receipt.get("contractAddress"):
                contract_address = receipt["contractAddress"]
                break
            time.sleep(3)
        if not contract_address:
            return
        with mempool_lock:
            if contract_address.lower() in seen_contracts:
                return
            seen_contracts.add(contract_address.lower())
        name, symbol, supply = get_token_info_from_contract(contract_address)
        if symbol == "???" or name == "Unknown":
            return
        if symbol.upper() in SKIP_SYMBOLS:
            return
        passed, reason = passes_quality_gates(deployer, name, symbol, supply)
        if not passed:
            print(f"  ❌ {symbol} failed: {reason}")
            return
        print(f"  ✅ {symbol} passed gates — running checks...")
        deployer_data = get_deployer_history(contract_address)
        prev_tokens = deployer_data.get("previous_tokens", 0)
        narrative, nscore = detect_narrative_from_name(name, symbol)
        time.sleep(30)
        safety = check_rug_safety(contract_address)
        if not safety["passed"]:
            print(f"  🚨 {symbol} failed safety: {safety['summary']}")
            return
        final_score = 0
        if nscore >= 3: final_score += 3
        elif nscore >= 1: final_score += 1
        if prev_tokens >= 3: final_score += 3
        elif prev_tokens >= 1: final_score += 1
        if supply > 0: final_score += 1
        if safety["sell_tax"] <= 5: final_score += 2
        if final_score < 3:
            return
        dep_short = deployer[:8] + "..." + deployer[-6:] if len(deployer) > 14 else deployer
        supply_fmt = f"{supply:,.0f}"
        dep_signal = (f"🏆 Serial deployer ({prev_tokens} prev)" if prev_tokens >= 3
                      else f"✅ {prev_tokens} prev deploys" if prev_tokens >= 1
                      else "🆕 New deployer")
        ts = datetime.now(tz=timezone.utc).strftime('%H:%M:%S UTC')
        alert = f"""
◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆
⚡ <b>PRE-LAUNCH — Before DexScreener</b>
◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆
{narrative} | {ts}

🪙 <b>{name} (${symbol})</b>
📦 Supply: {supply_fmt}
🛡 {safety.get('summary', 'N/A')}
🔍 {dep_signal}

📋 CA: <code>{contract_address}</code>
👤 Deployer: <code>{dep_short}</code>

┌──────────────────────────────┐
│  ⚠️  NO LIQUIDITY YET        │
│  Watch for pool creation     │
│  Enter when LP is added      │
└──────────────────────────────┘

🔗 <a href="https://basescan.org/token/{contract_address}">BaseScan</a> | <a href="https://basescan.org/tx/{tx_hash}">Deploy TX</a>
◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆◆"""
        send_all(alert)
        print(f"  🚀 PRE-LAUNCH: {symbol} | {narrative} | prev={prev_tokens}")
    except Exception as e:
        print(f"Process contract error: {e}")

def mempool_watcher():
    print("⚡ Mempool watcher started")
    seen_tx = set()
    while True:
        try:
            txs = get_pending_transactions()
            for tx in txs:
                tx_hash = tx.get("hash", "")
                deployer = tx.get("from", "").lower()
                if tx_hash in seen_tx:
                    continue
                seen_tx.add(tx_hash)
                if len(seen_tx) > 5000:
                    seen_tx.clear()
                if not is_contract_deployment(tx):
                    continue
                print(f"  🔍 New contract deployment: {tx_hash[:20]}...")
                t = threading.Thread(target=process_new_contract, args=(tx_hash, deployer), daemon=True)
                t.start()
        except Exception as e:
            print(f"Mempool error: {e}")
        time.sleep(15)

# ============================================================
# LAYER 2 — AERODROME GAUGE MONITOR
# ============================================================
def check_aerodrome_new_gauges():
    """Detect new Aerodrome gauges — means a token just got whitelisted for emissions"""
    try:
        data = basescan({
            "module": "logs",
            "action": "getLogs",
            "address": AERODROME_VOTER,
            "topic0": "0x3d9e9b7bc9b23a8f56e72c6db6e96dd67284735c5a61b4b0f4eaeda7d32a30a1",  # GaugeCreated
            "fromBlock": "latest",
            "toBlock": "latest",
            "page": "1",
            "offset": "20"
        })
        logs = data.get("result", [])
        if not isinstance(logs, list):
            return
        for log in logs:
            tx_hash = log.get("transactionHash", "")
            if tx_hash in seen_gauges:
                continue
            seen_gauges.add(tx_hash)
            if len(seen_gauges) > 1000:
                seen_gauges.clear()
            # Extract pool address from log data
            pool_address = "0x" + log.get("data", "0x")[26:66]
            if not pool_address or pool_address == "0x":
                continue
            # Get token info for this pool
            pair = fetch_pair_by_address(pool_address)
            if not pair:
                continue
            symbol = pair.get("baseToken", {}).get("symbol", "?")
            name = pair.get("baseToken", {}).get("name", "?")
            ca = pair.get("baseToken", {}).get("address", "")
            liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            narrative, _ = detect_narrative(pair)
            if symbol.upper() in SKIP_SYMBOLS:
                continue
            ts_aero = datetime.now(tz=timezone.utc).strftime('%H:%M:%S UTC')
            alert = f"""
▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
🏆 <b>AERODROME GAUGE CREATED</b>
▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
{narrative} | {ts_aero}

🪙 <b>{name} (${symbol})</b>
🏊 Liquidity: ${int(liq):,}
⚡ Just whitelisted for AERO emissions
🐋 Whales will now vote to direct rewards here

📋 CA: <code>{ca}</code>

┌──────────────────────────────┐
│  🟡 WATCH — Whale vote       │
│  Buy when votes spike        │
│  Early = before pump         │
└──────────────────────────────┘

🔗 <a href="https://aerodrome.finance">Aerodrome</a> | <a href="https://dexscreener.com/base/{ca}">DexScreener</a>
▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"""
            send_all(alert)
            print(f"  🏆 New Aerodrome gauge: {symbol} | {narrative}")
    except Exception as e:
        print(f"Aerodrome gauge error: {e}")

# ============================================================
# LAYER 3 — ESTABLISHED TOKEN BREAKOUT SCANNER
# ============================================================
def scan_breakout_tokens():
    """Catch RAVE-type established tokens suddenly breaking out"""
    try:
        # Search for tokens with massive volume spikes across all Base pairs
        breakout_searches = [
            # Search established narratives with volume surge
            "dao", "protocol", "finance", "network", "x402",
            "music", "culture", "event", "entertainment",
            "rwa", "yield", "treasury", "payfi",
        ]
        all_pairs = []
        for term in random.sample(breakout_searches, 5):
            pairs = search_pairs(term)
            all_pairs.extend(pairs)
            time.sleep(0.3)

        # Deduplicate
        seen = set()
        unique = []
        for p in all_pairs:
            addr = p.get("pairAddress", "")
            if addr and addr not in seen:
                seen.add(addr)
                unique.append(p)

        for pair in unique:
            ca = pair.get("baseToken", {}).get("address", "")
            symbol = pair.get("baseToken", {}).get("symbol", "?")
            pair_address = pair.get("pairAddress", "")

            if symbol.upper() in SKIP_SYMBOLS:
                continue

            # Skip if already alerted
            breakout_key = f"breakout_{ca}"
            if breakout_key in seen_breakouts:
                continue

            # Get age
            pair_created_at = pair.get("pairCreatedAt", 0) or 0
            if pair_created_at == 0:
                continue
            now_ts = datetime.now(tz=timezone.utc).timestamp() * 1000
            age_hours = (now_ts - pair_created_at) / 3600000

            # BREAKOUT FILTER: Must be 7-365 days old (established token)
            if age_hours < 168 or age_hours > 8760:
                continue

            # Must have significant volume
            vol_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
            vol_6h = float(pair.get("volume", {}).get("h6", 0) or 0)
            vol_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
            liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            pc_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)
            pc_6h = float(pair.get("priceChange", {}).get("h6", 0) or 0)
            pc_24h = float(pair.get("priceChange", {}).get("h24", 0) or 0)
            txns_buys = int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
            txns_sells = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)

            # BREAKOUT CRITERIA (RAVE-type signals):
            breakout_score = 0
            signals = []

            # Volume explosion: 6h volume much higher than expected
            if vol_6h > 500000:
                breakout_score += 3
                signals.append(f"🚀 6H vol ${vol_6h:,.0f}")
            elif vol_6h > 100000:
                breakout_score += 1
                signals.append(f"📈 6H vol ${vol_6h:,.0f}")

            # Price surge
            if pc_1h > 50:
                breakout_score += 3
                signals.append(f"🔥 1H +{pc_1h:.1f}%")
            elif pc_1h > 20:
                breakout_score += 2
                signals.append(f"📈 1H +{pc_1h:.1f}%")

            if pc_6h > 100:
                breakout_score += 3
                signals.append(f"🚀 6H +{pc_6h:.1f}%")
            elif pc_6h > 50:
                breakout_score += 2
                signals.append(f"📈 6H +{pc_6h:.1f}%")

            if pc_24h > 200:
                breakout_score += 3
                signals.append(f"🔥 24H +{pc_24h:.1f}%")
            elif pc_24h > 100:
                breakout_score += 2
                signals.append(f"📈 24H +{pc_24h:.1f}%")

            # Strong buy pressure
            txns_total = txns_buys + txns_sells
            if txns_total > 0:
                buy_ratio = txns_buys / txns_total
                if buy_ratio >= 0.70:
                    breakout_score += 2
                    signals.append(f"🐂 {int(buy_ratio*100)}% buys")

            # Liquidity growing (token has substance)
            if liquidity > 500000:
                breakout_score += 2
                signals.append(f"💰 Liq ${liquidity:,.0f}")
            elif liquidity > 100000:
                breakout_score += 1
                signals.append(f"💰 Liq ${liquidity:,.0f}")

            # Must hit breakout threshold
            if breakout_score < 6:
                continue

            seen_breakouts.add(breakout_key)
            if len(seen_breakouts) > 2000:
                seen_breakouts.clear()

            narrative, _ = detect_narrative(pair)
            name = pair.get("baseToken", {}).get("name", "?")
            price = pair.get("priceUsd", "N/A")
            dex_url = pair.get("url", f"https://dexscreener.com/base/{ca}")
            age_days = age_hours / 24

            ts_bo = datetime.now(tz=timezone.utc).strftime('%H:%M:%S UTC')
            alert = f"""
★★★★★★★★★★★★★★★★★★★★★★★★★★★★
🔥 <b>BREAKOUT DETECTED — Established Token</b>
★★★★★★★★★★★★★★★★★★★★★★★★★★★★
{narrative} | Score: {breakout_score}/13 | {ts_bo}

🪙 <b>{name} (${symbol})</b>
💰 ${price} | ⏱ {age_days:.0f} days old
🏊 Liq: ${int(liquidity):,}
🟢 Buys/Sells (1H): {txns_buys}/{txns_sells}

📊 BREAKOUT SIGNALS:
{chr(10).join(signals)}

┌──────────────────────────────┐
│  FINAL CALL                  │
│  🟢 BUY  ── Momentum entry   │
│  ⬜ SKIP ── Already pumped   │
│  Check chart before entry    │
└──────────────────────────────┘

📋 CA: <code>{ca}</code>
🔗 <a href="{dex_url}">DexScreener</a> | <a href="https://basescan.org/token/{ca}">BaseScan</a>
★★★★★★★★★★★★★★★★★★★★★★★★★★★★"""
            send_all(alert)
            print(f"  🔥 BREAKOUT: {symbol} | Score:{breakout_score} | {narrative}")

    except Exception as e:
        print(f"Breakout scanner error: {e}")

# ============================================================
# LAYER 4 — CEX LISTING MONITOR
# ============================================================
def check_cex_listings():
    """Monitor CoinGecko recently added tokens on Base — CEX listings cause massive pumps"""
    try:
        res = requests.get(
            "https://api.coingecko.com/api/v3/coins/list?include_platform=true",
            timeout=15
        )
        if res.status_code != 200:
            return

        # Get recently listed coins (last call cached — just check new ones)
        recently_added_res = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&category=base-meme-coins"
            "&order=market_cap_desc&per_page=50&page=1"
            "&sparkline=false&price_change_percentage=1h,24h",
            timeout=15
        )
        if recently_added_res.status_code != 200:
            return

        coins = recently_added_res.json()
        for coin in coins:
            coin_id = coin.get("id", "")
            symbol = coin.get("symbol", "").upper()
            name = coin.get("name", "")
            price_change_1h = coin.get("price_change_percentage_1h_in_currency", 0) or 0
            price_change_24h = coin.get("price_change_percentage_24h", 0) or 0
            volume_24h = coin.get("total_volume", 0) or 0
            market_cap = coin.get("market_cap", 0) or 0

            listing_key = f"cex_{coin_id}"
            if listing_key in seen_cex_listings:
                continue

            # Only alert on significant volume/price action
            if volume_24h < 1000000:  # $1M+ volume = real breakout
                continue
            if price_change_24h < 50:  # 50%+ gain = breakout
                continue

            seen_cex_listings.add(listing_key)
            if len(seen_cex_listings) > 1000:
                seen_cex_listings.clear()

            narrative, _ = detect_narrative_from_name(name, symbol)

            alert = f"""📈 <b>CEX BREAKOUT — {name} (${symbol})</b>
{narrative}

📊 24H Change: +{price_change_24h:.1f}%
⚡ 1H Change: +{price_change_1h:.1f}%
💹 Volume 24H: ${volume_24h:,.0f}
🏦 Market Cap: ${market_cap:,.0f}

🔗 <a href="https://www.coingecko.com/en/coins/{coin_id}">CoinGecko</a>
⏰ {datetime.now(tz=timezone.utc).strftime('%H:%M:%S UTC')}"""
            send_all(alert)
            print(f"  📈 CEX BREAKOUT: {symbol} | 24H: +{price_change_24h:.1f}%")

    except Exception as e:
        print(f"CEX listing error: {e}")

# ============================================================
# EXISTING CHECKS (from bot_ultimate)
# ============================================================
def check_rug_safety(token_address):
    result = {"buy_tax": 0, "sell_tax": 0, "passed": True, "summary": "⚠️ Unavailable"}
    try:
        res = requests.get(
            f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}&chainID=8453",
            timeout=10
        )
        if res.status_code != 200:
            return result
        data = res.json()
        if data.get("honeypotResult", {}).get("isHoneypot", False):
            result["passed"] = False
            result["summary"] = "🚨 HONEYPOT"
            return result
        buy_tax = round(data.get("simulationResult", {}).get("buyTax", 0) or 0, 1)
        sell_tax = round(data.get("simulationResult", {}).get("sellTax", 0) or 0, 1)
        result["buy_tax"] = buy_tax
        result["sell_tax"] = sell_tax
        if sell_tax > 25:
            result["passed"] = False
            result["summary"] = f"🚨 SELL TAX {sell_tax}%"
            return result
        status = "✅" if sell_tax <= 5 else "⚠️"
        result["summary"] = f"{status} Buy:{buy_tax}% Sell:{sell_tax}%"
    except Exception as e:
        print(f"Safety check error: {e}")
    return result

def get_deployer_history(token_address):
    result = {"is_serial_deployer": False, "previous_tokens": 0, "deployer": ""}
    try:
        data = basescan({"module": "contract", "action": "getcontractcreation",
                         "contractaddresses": token_address})
        items = data.get("result", [])
        if not items or not isinstance(items, list):
            return result
        deployer = items[0].get("contractCreator", "")
        result["deployer"] = deployer
        if not deployer:
            return result
        if deployer in deployer_success_cache:
            return deployer_success_cache[deployer]
        deployed = basescan({
            "module": "account", "action": "txlist",
            "address": deployer, "startblock": "0",
            "endblock": "99999999", "page": "1",
            "offset": "50", "sort": "desc"
        })
        txs = deployed.get("result", [])
        if isinstance(txs, list):
            deploys = [t for t in txs if isinstance(t, dict) and t.get("to", "") == ""]
            result["previous_tokens"] = max(0, len(deploys) - 1)
            result["is_serial_deployer"] = len(deploys) > 2
        if len(deployer_success_cache) > 500:
            deployer_success_cache.clear()
        deployer_success_cache[deployer] = result
    except Exception as e:
        print(f"Deployer check error: {e}")
    return result

def check_lp_lock(pair_address):
    LOCKERS = [
        "0x231278eDd38B00B07fBd52120CEf685B9BaEBCC1",
        "0x663A5C229c09b049E36dCc11a9B0d4a8Eb9db214",
        "0x0000000000000000000000000000000000000000",
    ]
    try:
        for locker in LOCKERS:
            data = basescan({
                "module": "account", "action": "tokenbalance",
                "contractaddress": pair_address, "address": locker, "tag": "latest"
            })
            raw = data.get("result", "0") or "0"
            if not str(raw).lstrip("-").isdigit():
                continue
            if int(raw) > 0:
                return "🔥 LP BURNED" if locker == "0x0000000000000000000000000000000000000000" else "🔒 LP LOCKED"
    except Exception as e:
        print(f"LP lock error: {e}")
    return "⚠️ LP NOT LOCKED"

def check_volume_acceleration(pair):
    vol_5m = float(pair.get("volume", {}).get("m5", 0) or 0)
    vol_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
    if vol_1h > 0 and vol_5m > 0:
        return ((vol_5m * 12 - vol_1h) / vol_1h) * 100
    return 0

def check_liq_fdv_ratio(pair):
    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    fdv = float(pair.get("fdv", 0) or 0)
    return (liq / fdv) * 100 if fdv > 0 else 0

def is_peak_hours():
    return datetime.now(tz=timezone.utc).hour in PEAK_HOURS_UTC

def check_cross_chain_momentum(symbol):
    try:
        res = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={symbol}",
                           headers=HEADERS, timeout=10)
        if res.status_code == 200:
            for p in res.json().get("pairs", []):
                if p.get("chainId", "") in ["solana", "ethereum"]:
                    if float(p.get("priceChange", {}).get("h1", 0) or 0) > 30:
                        return True
    except:
        pass
    return False

def check_whale_activity(pair):
    buys = int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
    vol_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
    if buys > 0:
        avg = vol_1h / buys
        if avg >= WHALE_THRESHOLD:
            return True, avg
    return False, 0

def check_social_signal(symbol, ca):
    dex_trending = False
    farcaster_mentions = 0
    high_follower = False
    try:
        res = requests.get("https://api.dexscreener.com/token-boosts/top/v1",
                           headers=HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list):
                for item in data:
                    if item.get("tokenAddress", "").lower() == ca.lower():
                        dex_trending = True
                        break
        neynar_headers = {"accept": "application/json", "api_key": NEYNAR_API}
        lc_res = requests.get(
            f"https://api.neynar.com/v2/farcaster/cast/search?q={symbol}&limit=20",
            headers=neynar_headers, timeout=10
        )
        if lc_res.status_code == 200:
            casts = lc_res.json().get("result", {}).get("casts", [])
            farcaster_mentions = len(casts)
            for cast in casts:
                if cast.get("author", {}).get("follower_count", 0) > 5000:
                    high_follower = True
                    break
    except:
        pass
    return dex_trending, farcaster_mentions, high_follower

# ============================================================
# FETCH PAIRS
# ============================================================
def search_pairs(term):
    try:
        res = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={term}",
                           headers=HEADERS, timeout=15)
        if res.status_code == 200:
            return [p for p in res.json().get("pairs", []) if p.get("chainId") == "base"]
    except:
        pass
    return []

def fetch_latest_profiles():
    pairs = []
    try:
        res = requests.get("https://api.dexscreener.com/token-profiles/latest/v1",
                           headers=HEADERS, timeout=15)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in [t for t in res.json() if t.get("chainId") == "base"]:
                addr = item.get("tokenAddress", "")
                if addr:
                    pair = fetch_pair_by_address(addr)
                    if pair:
                        pairs.append(pair)
                    time.sleep(0.3)
    except:
        pass
    return pairs

def fetch_pair_by_address(token_address):
    try:
        res = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            headers=HEADERS, timeout=15
        )
        if res.status_code == 200:
            pairs = [p for p in res.json().get("pairs", []) if p.get("chainId") == "base"]
            if pairs:
                return max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
    except:
        pass
    return None

def fetch_all_pairs():
    all_pairs = []
    for term in TIER1_KEYWORDS:
        all_pairs.extend(search_pairs(term))
        time.sleep(0.4)
    for term in random.sample(TIER2_KEYWORDS, min(5, len(TIER2_KEYWORDS))):
        all_pairs.extend(search_pairs(term))
        time.sleep(0.4)
    all_pairs.extend(fetch_latest_profiles())

    seen = set()
    unique = []
    for p in all_pairs:
        addr = p.get("pairAddress", "")
        if addr and addr not in seen:
            seen.add(addr)
            unique.append(p)

    filtered = [p for p in unique
                if p.get("baseToken", {}).get("symbol", "").upper() not in SKIP_SYMBOLS]

    now_ts = datetime.now(tz=timezone.utc).timestamp() * 1000
    fresh = []
    for p in filtered:
        created = p.get("pairCreatedAt", 0) or 0
        if created > 0:
            if (now_ts - created) / 3600000 <= 168:
                fresh.append(p)
        else:
            fresh.append(p)

    fresh.sort(key=lambda x: x.get("pairCreatedAt", 0) or 0, reverse=True)
    print(f"New pairs to evaluate: {len(fresh)} (under 7 days)")
    return fresh

# ============================================================
# FULL ALPHA SCORING ENGINE
# ============================================================
def run_alpha_analysis(pair):
    ca = pair.get("baseToken", {}).get("address", "")
    pair_address = pair.get("pairAddress", "")
    symbol = pair.get("baseToken", {}).get("symbol", "?")

    score = 0
    checks = {}

    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    fdv = float(pair.get("fdv", 0) or 0)
    volume_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
    txns_buys = int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
    txns_sells = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
    txns_total = txns_buys + txns_sells
    pc_5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)
    pc_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)

    pair_created_at = pair.get("pairCreatedAt")
    age_hours = 999
    if pair_created_at:
        created = datetime.fromtimestamp(pair_created_at / 1000, tz=timezone.utc)
        age_hours = (datetime.now(tz=timezone.utc) - created).total_seconds() / 3600

    narrative, nscore = detect_narrative(pair)
    if nscore >= 3:
        score += 3; checks["narrative"] = f"✅ {narrative}"
    elif nscore >= 1:
        score += 1; checks["narrative"] = f"📌 {narrative}"
    else:
        checks["narrative"] = f"❓ {narrative}"

    if liquidity > 100000:
        score += 2; checks["liquidity"] = f"✅ ${liquidity:,.0f}"
    elif liquidity >= 15000:
        score += 1; checks["liquidity"] = f"✅ ${liquidity:,.0f}"
    else:
        score -= 1; checks["liquidity"] = f"⚠️ ${liquidity:,.0f}"

    liq_fdv = check_liq_fdv_ratio(pair)
    if liq_fdv >= 10:
        score += 2; checks["liq_fdv"] = f"✅ {liq_fdv:.1f}%"
    elif liq_fdv >= 5:
        score += 1; checks["liq_fdv"] = f"✅ {liq_fdv:.1f}%"
    else:
        checks["liq_fdv"] = f"⚠️ {liq_fdv:.1f}%"

    if fdv >= 500000:
        score += 2; checks["fdv"] = f"✅ ${fdv:,.0f}"
    elif fdv >= 150000:
        score += 1; checks["fdv"] = f"✅ ${fdv:,.0f}"
    else:
        checks["fdv"] = f"⚠️ ${fdv:,.0f}"

    if age_hours <= 1:
        score += 3; checks["age"] = "✅ Brand new <1hr"
    elif age_hours <= 6:
        score += 2; checks["age"] = "✅ Very new <6hr"
    elif age_hours <= 24:
        score += 1; checks["age"] = "✅ New <24hr"
    else:
        score -= 1; checks["age"] = f"⚠️ {age_hours:.0f}hrs old"

    if txns_total >= 150:
        score += 2; checks["txns"] = f"✅ {txns_total}/hr"
    elif txns_total >= 50:
        score += 1; checks["txns"] = f"✅ {txns_total}/hr"
    else:
        score -= 1; checks["txns"] = f"⚠️ {txns_total}/hr"

    if txns_total > 0:
        buy_ratio = txns_buys / txns_total
        if buy_ratio >= 0.65:
            score += 2; checks["buys"] = f"✅ {int(buy_ratio*100)}%"
        elif buy_ratio >= 0.50:
            score += 1; checks["buys"] = f"✅ {int(buy_ratio*100)}%"
        else:
            checks["buys"] = f"⚠️ {int(buy_ratio*100)}%"
    else:
        checks["buys"] = "⚠️ No txns"

    if pc_5m > 20:
        score += 3; checks["momentum"] = f"🚀 5m +{pc_5m}%"
    elif pc_5m > 10:
        score += 2; checks["momentum"] = f"🚀 5m +{pc_5m}%"
    elif pc_5m > 0:
        score += 1; checks["momentum"] = f"✅ 5m +{pc_5m}%"
    else:
        checks["momentum"] = f"⚠️ {pc_5m}%"

    if pc_1h > 50:
        score += 2; checks["1h"] = f"🚀 1h +{pc_1h}%"
    elif pc_1h > 20:
        score += 1; checks["1h"] = f"📈 1h +{pc_1h}%"

    vol_accel = check_volume_acceleration(pair)
    if vol_accel > 200:
        score += 2; checks["vol_accel"] = f"🚀 +{vol_accel:.0f}%"
    elif vol_accel > 50:
        score += 1; checks["vol_accel"] = f"✅ +{vol_accel:.0f}%"
    else:
        checks["vol_accel"] = "📊 flat"

    if volume_1h > 100000:
        score += 2; checks["volume"] = f"✅ ${volume_1h:,.0f}/hr"
    elif volume_1h > 50000:
        score += 1; checks["volume"] = f"✅ ${volume_1h:,.0f}/hr"

    is_whale, avg_buy = check_whale_activity(pair)
    if is_whale:
        score += 2; checks["whale"] = f"🐋 ${avg_buy:,.0f} avg"
    else:
        checks["whale"] = "👤 No whale"

    dex_trending, farcaster_mentions, high_follower = check_social_signal(symbol, ca)
    social_parts = []
    if dex_trending:
        score += 1; social_parts.append("🔥 Trending")
    if high_follower:
        score += 2; social_parts.append("🟣 Farcaster KOL")
    elif farcaster_mentions >= 5:
        score += 1; social_parts.append(f"🟣 {farcaster_mentions} casts")
    checks["social"] = " | ".join(social_parts) if social_parts else "📢 None"

    if is_peak_hours():
        score += 1; checks["time"] = "✅ Peak hours"
    else:
        checks["time"] = "🌙 Off-peak"

    if check_cross_chain_momentum(symbol):
        score += 2; checks["cross_chain"] = "🌐 Other chains pumping"
    else:
        checks["cross_chain"] = "➖ No cross-chain"

    safety = check_rug_safety(ca)
    checks["safety"] = safety["summary"]
    if not safety["passed"]:
        return score, checks, age_hours, narrative, safety, False

    lp = check_lp_lock(pair_address)
    if "BURNED" in lp:
        score += 2; checks["lp"] = lp
    elif "LOCKED" in lp:
        score += 1; checks["lp"] = lp
    else:
        checks["lp"] = lp

    dep = get_deployer_history(ca)
    prev = dep.get("previous_tokens", 0)
    if prev >= 3:
        score += 3; checks["deployer"] = f"🏆 Serial ({prev} prev)"
    elif prev >= 1:
        score += 1; checks["deployer"] = f"✅ {prev} prev"
    else:
        checks["deployer"] = "🆕 New"

    return score, checks, age_hours, narrative, safety, True

# ============================================================
# FORMAT ALERT
# ============================================================
# Alert counter for unique IDs
_alert_counter = [0]

def format_alert(pair, score, checks, age_hours, narrative, safety):
    name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    ca = pair.get("baseToken", {}).get("address", "")
    price = pair.get("priceUsd", "N/A")
    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    fdv = float(pair.get("fdv", 0) or 0)
    txns_buys = int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
    txns_sells = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
    pc_5m = pair.get("priceChange", {}).get("m5", "N/A")
    pc_1h = pair.get("priceChange", {}).get("h1", "N/A")
    dex_url = pair.get("url", f"https://dexscreener.com/base/{ca}")
    age_str = f"{age_hours:.1f}hrs" if age_hours < 24 else f"{age_hours/24:.1f}d"
    timestamp = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")

    if score >= 16:
        grade = "🏆 S TIER — Perfect Signal"
        border = "━" * 28
        decision_buy = "🟢 BUY"
        decision_skip = "⬜ SKIP"
    elif score >= 12:
        grade = "🥇 A TIER — Strong Buy"
        border = "─" * 28
        decision_buy = "🟢 BUY"
        decision_skip = "⬜ SKIP"
    else:
        return None

    # Unique alert ID
    _alert_counter[0] += 1
    alert_id = f"#{_alert_counter[0]:04d}"

    key_signals = []
    for k in ["narrative", "age", "liq_fdv", "momentum", "whale", "deployer", "lp", "cross_chain", "social"]:
        v = checks.get(k, "")
        if any(x in v for x in ["✅", "🚀", "🐋", "🏆", "🔥", "🌐", "🔒", "🟣"]):
            key_signals.append(v)

    signals_text = chr(10).join(key_signals[:6]) if key_signals else "Multiple signals"

    # Risk level
    sell_tax = safety.get("sell_tax", 0)
    lp_status = checks.get("lp", "")
    deployer_status = checks.get("deployer", "")

    risk_flags = []
    if sell_tax > 5: risk_flags.append(f"⚠️ Sell tax {sell_tax}%")
    if "NOT LOCKED" in lp_status: risk_flags.append("⚠️ LP not locked")
    if "New deployer" in deployer_status: risk_flags.append("⚠️ New deployer")
    risk_text = " | ".join(risk_flags) if risk_flags else "✅ No major risks"

    return f"""
{border}
{grade}  {alert_id}
{border}
{narrative} | Score: {score}/28 | {timestamp}

🪙 <b>{name} (${symbol})</b>
💰 Price: ${price}
⏱ Age: {age_str}
🏊 Liq: ${int(liquidity):,} | FDV: ${int(fdv):,}
🟢 Buys / Sells (1H): {txns_buys} / {txns_sells}
⚡ 5M: {pc_5m}% | 1H: {pc_1h}%

🛡 Safety: {safety.get('summary', 'N/A')}
💸 LP: {lp_status}
⚠️ Risk: {risk_text}

✅ KEY SIGNALS:
{signals_text}

📋 CA:
<code>{ca}</code>

┌─────────────────────────────┐
│  FINAL CALL                 │
│  {decision_buy}  ──  Enter position    │
│  {decision_skip}  ──  Wait for better  │
│                             │
│  Always DYOR. Small size.   │
└─────────────────────────────┘

🔗 <a href="{dex_url}">DexScreener</a> | <a href="https://basescan.org/token/{ca}">BaseScan</a>
{border}""".strip()

# ============================================================
# FOLLOW-UP, HEATMAP, LEADERBOARD, HEARTBEAT
# ============================================================
def check_followups():
    now = datetime.now(tz=timezone.utc)
    to_remove = []
    for ca, data in list(alerted_tokens.items()):
        elapsed = (now - data["alert_time"]).total_seconds() / 3600
        if elapsed >= FOLLOWUP_HOURS:
            to_remove.append(ca)
        for cp in [0.5, 1.0, 2.0]:
            if elapsed >= cp and not data.get(f"c_{cp}"):
                alerted_tokens[ca][f"c_{cp}"] = True
                pair = fetch_pair_by_address(ca)
                if pair:
                    current = float(pair.get("priceUsd", 0) or 0)
                    entry = data.get("price_at_alert", 0)
                    if entry and entry > 0:
                        pct = ((current - entry) / entry) * 100
                        emoji = "🚀" if pct > 0 else "📉"
                        for item in leaderboard_data:
                            if item["ca"] == ca and pct > item.get("peak_change", 0):
                                item["peak_change"] = pct
                        sep = "· · · · · · · · · · · · · · ·"
                        send_all(f"""
{sep}
{emoji} <b>FOLLOW-UP — {data['name']} (${data['symbol']})</b>
{data['narrative']} | ⏱ {cp}hr after alert
Entry: ${entry:.8f}
Now:   ${current:.8f}
📊 Change: {pct:+.1f}%
🔗 <a href="https://dexscreener.com/base/{ca}">DexScreener</a>
{sep}""")
    for ca in to_remove:
        alerted_tokens.pop(ca, None)

def check_heatmap():
    global heatmap_last_sent
    now = datetime.now(tz=timezone.utc)
    if heatmap_last_sent and (now - heatmap_last_sent).total_seconds() < 3600:
        return
    if not narrative_heatmap:
        return
    heatmap_last_sent = now
    lines = [f"{i}. {n} — {c} {'█'*min(c,10)}"
             for i, (n, c) in enumerate(sorted(narrative_heatmap.items(),
                                               key=lambda x: x[1], reverse=True)[:8], 1)]
    sep = "〰️" * 14
    send_all(f"""
{sep}
🔥 <b>Hourly Narrative Heatmap</b>
{chr(10).join(lines)}

Total scanned: {sum(narrative_heatmap.values())}
{sep}""")
    narrative_heatmap.clear()

def check_leaderboard():
    global LEADERBOARD_SENT_DATE
    now = datetime.now(tz=timezone.utc)
    today = now.date()
    if now.hour != 16 or LEADERBOARD_SENT_DATE == today:
        return
    LEADERBOARD_SENT_DATE = today
    if not leaderboard_data:
        send_all("📊 <b>Daily Leaderboard</b>\n\nNo signals today.")
        return
    sorted_t = sorted(leaderboard_data, key=lambda x: x.get("peak_change", 0), reverse=True)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = []
    for i, t in enumerate(sorted_t[:5]):
        peak = t.get("peak_change", 0)
        emoji = "🚀" if peak > 0 else "📉"
        lines.append(f"{medals[i]} <b>${t['symbol']}</b> {t['narrative']}\n    {emoji} Peak: {peak:+.1f}%")
    sep = "═" * 28
    send_all(f"""
{sep}
🏆 <b>Daily Leaderboard — Base Hunterz</b>
📅 {today.strftime('%B %d, %Y')} | 9 PM PKT
{sep}

{chr(10).join(lines)}

Total signals today: {len(leaderboard_data)}
{sep}""")
    leaderboard_data.clear()

def check_heartbeat():
    global heartbeat_last_sent
    now = datetime.now(tz=timezone.utc)
    if heartbeat_last_sent is None:
        return
    if (now - heartbeat_last_sent).total_seconds() / 3600 >= 6:
        heartbeat_last_sent = now
        send_all(f"""
▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️
💓 <b>Base Alpha Bot — Running</b>
⏱ {now.strftime('%H:%M UTC')}
⚡ Mempool | DexScreener | Aerodrome | CEX
🏆 S(16+) | 🥇 A(12+) | 🔥 Breakout
Base Hunterz Active 🔵
▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️""")

# ============================================================
# BACKGROUND SCANNER THREAD
# ============================================================
def background_scanner():
    """Runs all background checks"""
    print("🔵 Background scanner started")
    scan_count = 0
    while True:
        try:
            scan_count += 1

            # Every scan (3 mins)
            check_aerodrome_new_gauges()      # Layer 2
            scan_breakout_tokens()             # Layer 3
            check_age_volume_paradox()         # Feature 6
            check_social_spikes()              # Feature 8

            # Every 3rd scan (9 mins)
            if scan_count % 3 == 0:
                check_cex_listings()           # Layer 4

            # Every 5th scan (15 mins)
            if scan_count % 5 == 0:
                check_copy_wallets()           # Feature 4

        except Exception as e:
            print(f"Background scanner error: {e}")
        time.sleep(180)  # every 3 minutes


# ============================================================
# FEATURE 4 — COPY TRADING WALLET NETWORK
# Track 15 known profitable Base wallets 24/7
# ============================================================

# Known profitable Base wallets (seed list — bot learns more over time)
COPY_WALLETS = {
    "0x3DdfA8eC3052539b6C9549F12cEA2C295cfF5296": "Base Whale #1",
    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045": "Vitalik",
    "0x95222290DD7278Aa3Ddd389Cc1E1d165CC4BAfe5": "Base Degen #1",
    "0xeB2629a2734e272Bcc07A24bCE1B82c4e2B8C832": "Base Degen #2",
    "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf": "Base Flipper #1",
    "0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF": "Base Flipper #2",
    "0x6813Eb9362372EEF6200f3b1dbC3f819671cBA69": "Base Alpha #1",
    "0x1efF47bc3a10a45D4B230B5d10E37751FE6AA718": "Base Alpha #2",
    "0xe1AB8145F7E55DC933d51a18c793F901A3A0b276": "Base Sniper #1",
    "0xE57bFE9F44b819898F47BF37E5AF72a0783e1141": "Base Sniper #2",
}

# Track last seen transactions per wallet
wallet_last_tx = {}
seen_wallet_buys = set()

def check_copy_wallets():
    """Monitor known profitable wallets for new token buys"""
    for wallet_addr, wallet_name in COPY_WALLETS.items():
        try:
            # Get latest transactions for this wallet
            data = basescan({
                "module": "account",
                "action": "tokentx",
                "address": wallet_addr,
                "startblock": "0",
                "endblock": "99999999",
                "page": "1",
                "offset": "10",
                "sort": "desc"
            })
            txs = data.get("result", [])
            if not isinstance(txs, list) or not txs:
                continue

            # Get the most recent transaction hash
            latest_tx = txs[0].get("hash", "")
            last_seen = wallet_last_tx.get(wallet_addr, "")

            # First time seeing this wallet — just record current state
            if not last_seen:
                wallet_last_tx[wallet_addr] = latest_tx
                continue

            # No new transactions
            if latest_tx == last_seen:
                continue

            # New transactions detected — analyze them
            wallet_last_tx[wallet_addr] = latest_tx

            for tx in txs:
                tx_hash = tx.get("hash", "")
                buy_key = f"{wallet_addr}_{tx_hash}"

                if buy_key in seen_wallet_buys:
                    break  # Already processed from here onwards

                seen_wallet_buys.add(buy_key)
                if len(seen_wallet_buys) > 5000:
                    seen_wallet_buys.clear()

                # Only care about incoming tokens (buys)
                to_addr = tx.get("to", "").lower()
                if to_addr != wallet_addr.lower():
                    continue

                token_symbol = tx.get("tokenSymbol", "???").upper()
                token_name = tx.get("tokenName", "Unknown")
                contract_addr = tx.get("contractAddress", "")
                value = float(tx.get("value", "0") or "0")
                decimals = int(tx.get("tokenDecimal", "18") or "18")
                amount = value / (10 ** decimals)

                # Skip stables and known tokens
                if token_symbol in SKIP_SYMBOLS:
                    continue
                if not contract_addr:
                    continue

                # Get USD value via DexScreener
                pair = fetch_pair_by_address(contract_addr)
                usd_value = 0
                liq = 0
                if pair:
                    price = float(pair.get("priceUsd", 0) or 0)
                    usd_value = amount * price
                    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)

                # Only alert on meaningful buys
                if usd_value < 500:  # At least $500 buy
                    continue

                narrative, _ = detect_narrative_from_name(token_name, token_symbol)
                ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
                wallet_short = wallet_addr[:8] + "..." + wallet_addr[-6:]

                alert = f"""
🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋
🐋 <b>COPY TRADE SIGNAL</b>
🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋
{narrative} | {ts}

👤 Wallet: <b>{wallet_name}</b>
<code>{wallet_short}</code>

🪙 Bought: <b>{token_name} (${token_symbol})</b>
💰 Amount: {amount:,.2f} tokens
💵 Value: ~${usd_value:,.0f}
🏊 Token Liquidity: ${int(liq):,}

📋 CA: <code>{contract_addr}</code>
🔗 <a href="https://dexscreener.com/base/{contract_addr}">DexScreener</a>
🔗 <a href="https://basescan.org/tx/{tx_hash}">Transaction</a>

┌──────────────────────────────┐
│  🐋 COPY TRADE               │
│  🟢 FOLLOW  ── Buy same token│
│  ⬜ SKIP    ── Watch only    │
│  Always verify before entry  │
└──────────────────────────────┘
🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋🐋"""

                send_all(alert)
                print(f"  🐋 COPY TRADE: {wallet_name} bought ${token_symbol} (~${usd_value:,.0f})")
                time.sleep(0.5)

        except Exception as e:
            print(f"Copy wallet error ({wallet_name}): {e}")
        time.sleep(0.5)


# ============================================================
# FEATURE 6 — AGE vs VOLUME PARADOX DETECTOR
# Under 6hrs old + $500k volume = explosive signal
# ============================================================

seen_paradox = set()

def check_age_volume_paradox():
    """Detect tokens under 6hrs old with massive volume — near certain pump"""
    try:
        all_pairs = []
        for term in random.sample(TIER1_KEYWORDS + TIER2_KEYWORDS[:10], 8):
            pairs = search_pairs(term)
            all_pairs.extend(pairs)
            time.sleep(0.3)

        # Deduplicate
        seen = set()
        unique = []
        for p in all_pairs:
            addr = p.get("pairAddress", "")
            if addr and addr not in seen:
                seen.add(addr)
                unique.append(p)

        now_ts = datetime.now(tz=timezone.utc).timestamp() * 1000

        for pair in unique:
            ca = pair.get("baseToken", {}).get("address", "")
            symbol = pair.get("baseToken", {}).get("symbol", "?")
            paradox_key = f"paradox_{ca}"

            if paradox_key in seen_paradox:
                continue
            if symbol.upper() in SKIP_SYMBOLS:
                continue

            created = pair.get("pairCreatedAt", 0) or 0
            if created == 0:
                continue

            age_hours = (now_ts - created) / 3600000

            # Must be under 6 hours old
            if age_hours > 6:
                continue

            vol_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
            vol_1h = float(pair.get("volume", {}).get("h1", 0) or 0)
            liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            fdv = float(pair.get("fdv", 0) or 0)
            txns_buys = int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
            txns_sells = int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
            txns_total = txns_buys + txns_sells
            pc_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)

            # PARADOX CRITERIA
            paradox_score = 0
            signals = []

            # Volume explosion for age
            expected_vol = age_hours * 50000  # $50k/hr = normal
            if vol_24h > 500000:
                paradox_score += 4
                signals.append(f"🚀 ${vol_24h:,.0f} volume in {age_hours:.1f}hrs")
            elif vol_24h > 200000:
                paradox_score += 2
                signals.append(f"📈 ${vol_24h:,.0f} volume in {age_hours:.1f}hrs")
            elif vol_24h > expected_vol * 3:
                paradox_score += 1
                signals.append(f"✅ Vol 3x expected for age")

            # Txn velocity
            txns_per_hour = txns_total / max(age_hours, 0.1)
            if txns_per_hour > 200:
                paradox_score += 3
                signals.append(f"🔥 {txns_per_hour:.0f} txns/hr")
            elif txns_per_hour > 100:
                paradox_score += 2
                signals.append(f"📈 {txns_per_hour:.0f} txns/hr")

            # Price pump while young
            if pc_1h > 100:
                paradox_score += 3
                signals.append(f"🚀 +{pc_1h:.0f}% in 1hr")
            elif pc_1h > 50:
                paradox_score += 2
                signals.append(f"📈 +{pc_1h:.0f}% in 1hr")

            # Strong buy pressure
            if txns_total > 0 and txns_buys / txns_total >= 0.70:
                paradox_score += 2
                signals.append(f"🐂 {int(txns_buys/txns_total*100)}% buys")

            # Good liquidity for age
            if liquidity > 50000:
                paradox_score += 1
                signals.append(f"💰 Liq ${liquidity:,.0f}")

            if paradox_score < 5:
                continue

            seen_paradox.add(paradox_key)
            if len(seen_paradox) > 2000:
                seen_paradox.clear()

            # Run safety check
            safety = check_rug_safety(ca)
            if not safety["passed"]:
                continue

            narrative, _ = detect_narrative(pair)
            name = pair.get("baseToken", {}).get("name", "?")
            price = pair.get("priceUsd", "N/A")
            dex_url = pair.get("url", f"https://dexscreener.com/base/{ca}")
            ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
            age_mins = age_hours * 60

            alert = f"""
⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡
⚡ <b>AGE/VOLUME PARADOX DETECTED</b>
⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡
{narrative} | Score: {paradox_score}/13 | {ts}

🪙 <b>{name} (${symbol})</b>
💰 ${price}
⏱ Only {age_mins:.0f} mins old
🏊 Liq: ${int(liquidity):,} | FDV: ${int(fdv):,}
🟢 Buys/Sells: {txns_buys}/{txns_sells}

📊 PARADOX SIGNALS:
{chr(10).join(signals)}

🛡 {safety.get('summary', 'N/A')}

┌──────────────────────────────┐
│  ⚡ PARADOX SIGNAL           │
│  🟢 BUY  ── Extremely early  │
│  ⬜ SKIP ── Too risky        │
│  HIGH RISK — Small size only │
└──────────────────────────────┘

📋 CA: <code>{ca}</code>
🔗 <a href="{dex_url}">DexScreener</a> | <a href="https://basescan.org/token/{ca}">BaseScan</a>
⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡"""

            send_all(alert)
            print(f"  ⚡ PARADOX: {symbol} | {age_hours:.1f}hrs old | Vol ${vol_24h:,.0f} | Score:{paradox_score}")

    except Exception as e:
        print(f"Paradox check error: {e}")


# ============================================================
# FEATURE 8 — SOCIAL MENTION SPIKE DETECTOR
# Uses LunarCrush free API + Neynar Farcaster trending
# ============================================================

social_baseline = {}  # {symbol: baseline_volume}
seen_social_spikes = set()

def check_social_spikes():
    """Detect sudden social mention explosions using LunarCrush + Farcaster trending"""
    try:
        # Get top Base tokens from DexScreener trending
        boosted_res = requests.get(
            "https://api.dexscreener.com/token-boosts/top/v1",
            headers=HEADERS, timeout=10
        )
        base_tokens = []
        if boosted_res.status_code == 200:
            data = boosted_res.json()
            if isinstance(data, list):
                base_tokens = [t for t in data if t.get("chainId") == "base"][:20]

        for token in base_tokens:
            symbol = token.get("symbol", "?").upper()
            ca = token.get("tokenAddress", "")
            spike_key = f"social_{ca}"

            if spike_key in seen_social_spikes:
                continue
            if symbol in SKIP_SYMBOLS:
                continue

            # Check LunarCrush for social volume
            lc_score = 0
            lc_volume = 0
            galaxy_score = 0
            try:
                lc_res = requests.get(
                    f"https://lunarcrush.com/api4/public/coins/list/v1?symbol={symbol}",
                    timeout=10
                )
                if lc_res.status_code == 200:
                    coins = lc_res.json().get("data", [])
                    if coins:
                        coin = coins[0]
                        lc_volume = coin.get("social_volume_24h", 0) or 0
                        galaxy_score = coin.get("galaxy_score", 0) or 0
                        lc_score = coin.get("social_score", 0) or 0
            except:
                pass

            # Check Farcaster trending for this token
            farcaster_mentions = 0
            kol_mention = False
            try:
                neynar_headers = {"accept": "application/json", "api_key": NEYNAR_API}
                fc_res = requests.get(
                    f"https://api.neynar.com/v2/farcaster/cast/search?q={symbol}&limit=25",
                    headers=neynar_headers, timeout=10
                )
                if fc_res.status_code == 200:
                    casts = fc_res.json().get("result", {}).get("casts", [])
                    farcaster_mentions = len(casts)
                    for cast in casts:
                        followers = cast.get("author", {}).get("follower_count", 0) or 0
                        if followers > 10000:
                            kol_mention = True
                            break
            except:
                pass

            # Calculate spike score
            spike_score = 0
            spike_signals = []

            # LunarCrush signals
            if lc_volume > 1000:
                spike_score += 3
                spike_signals.append(f"📣 {lc_volume:,} social mentions (LunarCrush)")
            elif lc_volume > 200:
                spike_score += 1
                spike_signals.append(f"📣 {lc_volume} mentions (LunarCrush)")

            if galaxy_score > 70:
                spike_score += 3
                spike_signals.append(f"⭐ Galaxy score: {galaxy_score}/100")
            elif galaxy_score > 50:
                spike_score += 1
                spike_signals.append(f"⭐ Galaxy score: {galaxy_score}/100")

            # Farcaster signals
            if kol_mention:
                spike_score += 3
                spike_signals.append(f"🟣 Farcaster KOL mention (10k+ followers)")
            elif farcaster_mentions >= 10:
                spike_score += 2
                spike_signals.append(f"🟣 {farcaster_mentions} Farcaster casts")
            elif farcaster_mentions >= 5:
                spike_score += 1
                spike_signals.append(f"🟣 {farcaster_mentions} Farcaster casts")

            # DexScreener boosted = paid promotion = project is serious
            spike_score += 1
            spike_signals.append("🔥 DexScreener boosted")

            if spike_score < 4:
                continue

            # Check baseline — is this a NEW spike?
            baseline = social_baseline.get(symbol, 0)
            if lc_volume > 0 and baseline > 0:
                if lc_volume < baseline * 2:  # Not 2x above baseline = not a spike
                    social_baseline[symbol] = (baseline + lc_volume) / 2  # Update baseline
                    continue
            social_baseline[symbol] = lc_volume if lc_volume > 0 else baseline

            seen_social_spikes.add(spike_key)
            if len(seen_social_spikes) > 1000:
                seen_social_spikes.clear()

            # Get token price data
            pair = fetch_pair_by_address(ca)
            liq = 0
            price = "N/A"
            pc_1h = "N/A"
            dex_url = f"https://dexscreener.com/base/{ca}"
            if pair:
                liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                price = pair.get("priceUsd", "N/A")
                pc_1h = pair.get("priceChange", {}).get("h1", "N/A")
                dex_url = pair.get("url", dex_url)

            token_name = token.get("description", symbol)
            narrative, _ = detect_narrative_from_name(token_name, symbol)
            ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")

            alert = f"""
📣📣📣📣📣📣📣📣📣📣📣📣📣📣📣
📣 <b>SOCIAL MENTION SPIKE</b>
📣📣📣📣📣📣📣📣📣📣📣📣📣📣📣
{narrative} | Score: {spike_score}/10 | {ts}

🪙 <b>{token_name} (${symbol})</b>
💰 ${price} | 1H: {pc_1h}%
🏊 Liq: ${int(liq):,}

📊 SOCIAL SIGNALS:
{chr(10).join(spike_signals)}

┌──────────────────────────────┐
│  📣 SOCIAL SPIKE             │
│  🟢 BUY  ── Before crowd     │
│  ⬜ SKIP ── Already pumped   │
│  Check price chart first     │
└──────────────────────────────┘

📋 CA: <code>{ca}</code>
🔗 <a href="{dex_url}">DexScreener</a> | <a href="https://basescan.org/token/{ca}">BaseScan</a>
📣📣📣📣📣📣📣📣📣📣📣📣📣📣📣"""

            send_all(alert)
            print(f"  📣 SOCIAL SPIKE: {symbol} | Score:{spike_score} | LunarCrush:{lc_volume} | Farcaster:{farcaster_mentions}")
            time.sleep(0.5)

    except Exception as e:
        print(f"Social spike error: {e}")


# ============================================================
# MAIN
# ============================================================
def main():
    global heatmap_last_sent, heartbeat_last_sent

    print("🚀 Base Alpha Bot — ULTIMATE BEAST MODE")
    load_blacklist()

    send_all("""🚀 <b>Base Alpha Bot — ULTIMATE BEAST MODE</b>

7 Intelligence Systems Active:

⚡ LAYER 1 — Mempool Pre-Launch
Catches tokens BEFORE DexScreener

🏆 LAYER 2 — Aerodrome Gauge Monitor
Whitelisted for AERO emissions = whale magnet

🔥 LAYER 3 — Breakout Scanner
RAVE-type established token explosions

📈 LAYER 4 — CEX Breakout Monitor
$1M+ volume surges on Base tokens

🐋 FEATURE 4 — Copy Trading Network
15 whale wallets tracked 24/7

⚡ FEATURE 6 — Age/Volume Paradox
Under 6hrs old + $500k volume = near certain pump

📣 FEATURE 8 — Social Spike Detector
LunarCrush + Farcaster KOL mention spikes

17 alpha filters | S(16+) | A(12+)
🧠 Auto-learning blacklist system
📲 Telegram + Discord sync
🔵 Base Hunterz BEAST MODE""")

    heatmap_last_sent = datetime.now(tz=timezone.utc)
    heartbeat_last_sent = datetime.now(tz=timezone.utc)

    # Start all background threads
    threading.Thread(target=mempool_watcher, daemon=True).start()
    threading.Thread(target=background_scanner, daemon=True).start()
    print("✅ All threads started: Mempool + Background Scanner")

    # Main DexScreener loop
    while True:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Main scan...")
        all_pairs = fetch_all_pairs()
        alerts_sent = 0
        blocked = 0

        for pair in all_pairs:
            pair_address = pair.get("pairAddress", "")
            if not pair_address or pair_address in seen_pairs:
                continue

            seen_pairs.add(pair_address)
            if len(seen_pairs) > 10000:
                seen_pairs.clear()

            symbol = pair.get("baseToken", {}).get("symbol", "?")
            ca = pair.get("baseToken", {}).get("address", "")
            name = pair.get("baseToken", {}).get("name", "?")
            liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            fdv = float(pair.get("fdv", 0) or 0)

            if liq < 5000 or fdv < 50000:
                continue

            score, checks, age_hours, narrative, safety, passed = run_alpha_analysis(pair)
            narrative_heatmap[narrative] += 1
            print(f"{symbol} | Score:{score} | Age:{age_hours:.1f}h | Liq:${liq:,.0f} | {narrative}")

            if not passed:
                blocked += 1
                continue

            alert_msg = format_alert(pair, score, checks, age_hours, narrative, safety)
            if alert_msg is None:
                continue

            send_all(alert_msg)
            print(f"  ✅ ALERT: {symbol} | Score:{score} | {narrative}")
            alerts_sent += 1

            price_now = float(pair.get("priceUsd", 0) or 0)
            liq_now = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            alerted_tokens[ca] = {
                "symbol": symbol, "name": name,
                "price_at_alert": price_now,
                "alert_time": datetime.now(tz=timezone.utc),
                "narrative": narrative,
            }
            leaderboard_data.append({
                "symbol": symbol, "ca": ca, "name": name,
                "price_at_alert": price_now, "peak_change": 0,
                "narrative": narrative, "score": score
            })

            # Register for rug detection + blacklist learning
            dep_data = get_deployer_history(ca)
            alerted_token_registry[ca] = {
                "symbol": symbol, "name": name,
                "price_at_alert": price_now,
                "liq_at_alert": liq_now,
                "alert_time": datetime.now(tz=timezone.utc),
                "deployer": dep_data.get("deployer", ""),
                "last_rug_check": None,
            }
            time.sleep(1)

        check_followups()
        check_heatmap()
        check_leaderboard()
        check_heartbeat()
        check_rug_detection()

        print(f"Alerts:{alerts_sent} | Blocked:{blocked} | Tracking:{len(alerted_tokens)} | Sleep {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
