#!/usr/bin/env python3
"""
================================================================================
ouo.io 24/7 Automation & Telegram Control Center (Standalone All-in-One Engine)
================================================================================
"""

import os
import sys
import time
import json
import random
import string
import signal
import argparse
import threading
import subprocess
import urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor


# ── Background launcher (keeps Pterodactyl server "online") ──────────────────
def _relaunch_in_background():
    """If not already backgrounded, relaunch self with nohup and exit."""
    if os.environ.get("OUO_BACKGROUNDED") == "1":
        return  # Already running in background
    print("[*] Relaunching in background to keep server online...")
    env = os.environ.copy()
    env["OUO_BACKGROUNDED"] = "1"
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output.log")
    with open(log_path, "a") as logf:
        subprocess.Popen(
            [sys.executable] + sys.argv,
            env=env,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )
    print(f"[+] Background process started. Logs: {log_path}")
    sys.exit(0)

# ── Trigger background relaunch only for long-running commands ───────────────
_long_running_cmds = {"daemon", "bot", "all"}
if len(sys.argv) > 1 and sys.argv[1] in _long_running_cmds:
    _relaunch_in_background()

# ── Now import the rest ───────────────────────────────────────────────────────
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None
    REQUESTS_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

try:
    import telebot
    from telebot import types
    from telebot.apihelper import ApiTelegramException
    TELEBOT_AVAILABLE = True
except ImportError:
    telebot = None
    types = None
    ApiTelegramException = Exception
    TELEBOT_AVAILABLE = False

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ------------------------------------------------------------------------------
# 1. PATH RESOLUTION & CONFIGURATION
# ------------------------------------------------------------------------------
BASE_DIR = os.environ.get("OUO_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

def resolve_path(filename, legacy_path=None):
    local_p = os.path.join(BASE_DIR, filename)
    if not os.path.exists(local_p) and legacy_path and os.path.exists(legacy_path):
        return legacy_path
    return local_p

SESSION_FILE = resolve_path("ouo_session.json", "/home/ipx/ouo_session.json")
STATS_FILE   = resolve_path("campaign_stats.json", "/home/ipx/campaign_stats.json")
AUTH_FILE    = resolve_path("bot_auth.json", "/home/ipx/bot_auth.json")

PROXY_CONFIG = None
if os.environ.get('OUO_PROXY_SERVER'):
    PROXY_CONFIG = {
        'server':   os.environ.get('OUO_PROXY_SERVER'),
        'username': os.environ.get('OUO_PROXY_USER', 'res-us'),
        'password': os.environ.get('OUO_PROXY_PASS', '')
    }

VISITOR_PROXY_SERVER = os.environ.get('OUO_VISITOR_PROXY_SERVER', 'http://gw.proxyrise.com:443')
VISITOR_PROXY_PASS   = os.environ.get('OUO_VISITOR_PROXY_PASS',
                                       'pgw-08e31e1cc8432fd2889c36462754d0e839f5178a3b0991b8')

def get_rotating_visitor_proxy():
    cfg = {
        'server':   VISITOR_PROXY_SERVER,
        'username': 'res-us-rotate',
        'password': VISITOR_PROXY_PASS
    }
    return cfg, "🇺🇸 US"

VISITOR_PROXY_CONFIG = {
    'server':   VISITOR_PROXY_SERVER,
    'username': 'res-us-rotate',
    'password': VISITOR_PROXY_PASS
}

OUO_USERNAME    = os.environ.get('OUO_USERNAME',    "nxtzenmaker@gmail.com")
OUO_PASSWORD    = os.environ.get('OUO_PASSWORD',    "Zeno@123")
MASTER_PASSWORD = os.environ.get('MASTER_PASSWORD', "Raj Nandini is the heart of Rudra/Zeno")
BOT_TOKEN       = os.environ.get('BOT_TOKEN',       "8943552749:AAEq10g6coSpFo39pQHzJO0ja9K15VlVm6Y")
DEFAULT_VERCEL_APP = os.environ.get('VERCEL_APP_URL', "https://ouo-portal.vercel.app")
ADMIN_CHAT_ID   = os.environ.get('ADMIN_CHAT_ID',   "8321630022")
USER_AGENT      = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")

HOURLY_CHECK_INTERVAL = 300
NUM_WORKERS    = int(os.environ.get("OUO_WORKERS", 2))
SESSION_LIFETIME = 86400

stats_lock     = threading.Lock()
shortener_lock = threading.Lock()
daemon_running = False

# ------------------------------------------------------------------------------
# 2. STATS ENGINE
# ------------------------------------------------------------------------------
DEFAULT_STATS = {
    "total_links_created": 0,
    "total_views_completed": 0,
    "failed_views": 0,
    "success_rate": "100%",
    "daemon_status": "⏸️ STOPPED",
    "daemon_started_at": 0,
    "daemon_started_str": "",
    "last_activity_time": 0,
    "last_activity_str": "None yet",
    "last_shortlink": "None yet",
    "last_target_url": "None yet",
    "last_visitor_country": "🇺🇸 US",
    "dashboard": {
        "views": "0",
        "earnings": "$0.00",
        "ecpm": "$0.00",
        "available": "$0.00",
        "last_checked_at": 0,
        "last_checked_str": "Never",
        "next_check_at": 0,
        "next_check_str": "Pending"
    },
    "history": []
}

def get_current_time_str(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

def _load_stats_unlocked():
    if not os.path.exists(STATS_FILE):
        return DEFAULT_STATS.copy()
    try:
        with open(STATS_FILE, 'r') as f:
            data = json.load(f)
        for k, v in DEFAULT_STATS.items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return DEFAULT_STATS.copy()

def _save_stats_unlocked(stats):
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        tmp = STATS_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, STATS_FILE)
    except Exception as e:
        print(f"[!] Error saving stats: {e}")

def load_stats():
    with stats_lock:
        if not os.path.exists(STATS_FILE):
            stats = DEFAULT_STATS.copy()
            stats["daemon_started_at"] = time.time()
            stats["daemon_started_str"] = get_current_time_str(stats["daemon_started_at"])
            _save_stats_unlocked(stats)
            return stats
        return _load_stats_unlocked()

def save_stats(stats):
    with stats_lock:
        _save_stats_unlocked(stats)

def record_link_created(short_url, target_url):
    with stats_lock:
        stats = _load_stats_unlocked()
        stats["total_links_created"] += 1
        stats["last_shortlink"]    = short_url
        stats["last_target_url"]   = target_url
        stats["last_activity_time"] = time.time()
        stats["last_activity_str"] = get_current_time_str(stats["last_activity_time"])
        _save_stats_unlocked(stats)

def record_view_result(success, short_url, dest_url, country="🇺🇸 US"):
    with stats_lock:
        stats = _load_stats_unlocked()
        if success:
            stats["total_views_completed"] += 1
        else:
            stats["failed_views"] += 1
        total = stats["total_views_completed"] + stats["failed_views"]
        stats["success_rate"] = f"{(stats['total_views_completed']/total*100):.1f}%" if total else "100%"
        stats["last_activity_time"]    = time.time()
        stats["last_activity_str"]     = get_current_time_str(stats["last_activity_time"])
        stats["last_visitor_country"]  = country
        entry = {
            "time":      get_current_time_str(),
            "shortlink": short_url,
            "country":   country,
            "status":    "SUCCESS" if success else "FAILED",
            "dest":      dest_url
        }
        history = stats.get("history", [])
        history.insert(0, entry)
        stats["history"] = history[:10]
        _save_stats_unlocked(stats)

def record_dashboard_stats(dashboard_data):
    with stats_lock:
        stats = _load_stats_unlocked()
        now = time.time()
        next_check = now + HOURLY_CHECK_INTERVAL
        stats["dashboard"] = {
            "views":            dashboard_data.get("views", "0"),
            "earnings":         dashboard_data.get("earnings", "$0.00"),
            "ecpm":             dashboard_data.get("ecpm", "$0.00"),
            "available":        dashboard_data.get("available", "$0.00"),
            "last_checked_at":  now,
            "last_checked_str": get_current_time_str(now),
            "next_check_at":    next_check,
            "next_check_str":   get_current_time_str(next_check)
        }
        _save_stats_unlocked(stats)

# ------------------------------------------------------------------------------
# 3. AUTHENTICATION & SESSION MANAGEMENT
# ------------------------------------------------------------------------------
def check_session_validity(headless=True):
    if not PLAYWRIGHT_AVAILABLE or not os.path.exists(SESSION_FILE):
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'])
            context = browser.new_context(proxy=PROXY_CONFIG, user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 720})
            with open(SESSION_FILE) as f:
                cookie_dict = json.load(f)
            context.add_cookies([{'name': k, 'value': v, 'domain': '.ouo.io', 'path': '/'}
                                  for k, v in cookie_dict.items()])
            page = context.new_page()
            page.goto('https://ouo.io/manage/home', wait_until='domcontentloaded', timeout=30000)
            for _ in range(15):
                time.sleep(1)
                if 'manage/home' in page.url or 'Dashboard' in page.title():
                    break
            is_valid = (('manage/home' in page.url or 'Dashboard' in page.title())
                        and 'auth/signin' not in page.url)
            browser.close()
            return is_valid
    except Exception as e:
        print(f"[!] Session check error: {e}")
        return False

def do_login(headless=True):
    if not PLAYWRIGHT_AVAILABLE:
        return False
    print("[*] Logging in to ouo.io...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled',
                      '--disable-infobars', '--disable-dev-shm-usage'],
                ignore_default_args=['--enable-automation'])
            context = browser.new_context(proxy=PROXY_CONFIG, user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 720})
            context.add_init_script('''
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            ''')
            page = context.new_page()
            page.goto('https://ouo.io/auth/signin', wait_until='domcontentloaded', timeout=35000)
            for _ in range(25):
                time.sleep(1)
                if 'Sign in' in page.title() or ('Just a moment' not in page.title() and page.title() != ''):
                    break
            if 'Sign in' not in page.title() and 'Just a moment' in page.title():
                browser.close()
                return False
            page.fill('input[name="username"]', OUO_USERNAME)
            page.fill('input[name="password"]', OUO_PASSWORD)
            remember = page.query_selector('input[name="remember-me"]')
            if remember and not remember.is_checked():
                remember.check()
            page.click('button[type="submit"]')
            for _ in range(15):
                time.sleep(1)
                if '/manage/home' in page.url or 'Dashboard' in page.title():
                    break
            if '/manage/home' in page.url or 'Dashboard' in page.title():
                cookies = context.cookies()
                auth_cookies = {c['name']: c['value'] for c in cookies}
                os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
                with open(SESSION_FILE, 'w') as f:
                    json.dump(auth_cookies, f, indent=2)
                browser.close()
                print(f"[🎉] Login successful! Session saved.")
                return True
            else:
                print(f"[-] Login failed. URL: {page.url}")
                browser.close()
                return False
    except Exception as e:
        print(f"[!] Login exception: {e}")
        return False

def ensure_valid_session():
    print("[*] Checking session health...")
    if check_session_validity():
        print("[+] Session valid.")
        return True
    print("[!] Session expired. Logging in fresh...")
    return do_login()

# ------------------------------------------------------------------------------
# 4. HOURLY DASHBOARD CHECKER
# ------------------------------------------------------------------------------
def check_ouo_dashboard_hourly(force=False):
    if not PLAYWRIGHT_AVAILABLE:
        return
    stats = load_stats()
    last_check = stats.get("dashboard", {}).get("last_checked_at", 0)
    if not force and (time.time() - last_check) < HOURLY_CHECK_INTERVAL and last_check > 0:
        return
    print("[*] Scraping ouo.io dashboard...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
                ignore_default_args=['--enable-automation'])
            context = browser.new_context(proxy=PROXY_CONFIG, user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 720})
            context.add_init_script('''
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            ''')
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE) as f:
                    cookie_dict = json.load(f)
                context.add_cookies([{'name': k, 'value': v, 'domain': '.ouo.io', 'path': '/'}
                                      for k, v in cookie_dict.items()])
            page = context.new_page()
            page.goto('https://ouo.io/manage/home', wait_until='domcontentloaded', timeout=35000)
            for _ in range(15):
                time.sleep(1)
                if 'manage/home' in page.url or 'Dashboard' in page.title():
                    break
            if 'auth/signin' in page.url or 'sign in' in page.title().lower():
                try:
                    page.fill('input[name="username"]', OUO_USERNAME)
                    page.fill('input[name="password"]', OUO_PASSWORD)
                    remember = page.query_selector('input[name="remember-me"]')
                    if remember and not remember.is_checked():
                        remember.check()
                    page.click('button[type="submit"]')
                    for _ in range(15):
                        time.sleep(1)
                        if '/manage/home' in page.url or 'Dashboard' in page.title():
                            break
                    if '/manage/home' in page.url or 'Dashboard' in page.title():
                        cookies = context.cookies()
                        with open(SESSION_FILE, 'w') as f:
                            json.dump({c['name']: c['value'] for c in cookies}, f, indent=2)
                except Exception as le:
                    print(f"[!] Relogin error: {le}")
            dash_data = {'views': '0', 'earnings': '$0.00', 'ecpm': '$0.00', 'available': '$0.00'}
            cards = page.query_selector_all('.col-sm-3, .col-xs-6, .panel')
            for c in cards:
                lines = [l.strip() for l in c.inner_text().split('\n') if l.strip()]
                if len(lines) >= 2:
                    val, label = lines[0], lines[1]
                    if label.lower() == 'views':
                        dash_data['views'] = val
                    elif label.lower() == 'earnings':
                        dash_data['earnings'] = val
                    elif label.lower() == 'ecpm':
                        dash_data['ecpm'] = val
                    elif 'total available' in label.lower():
                        dash_data['available'] = val
            browser.close()
            old_dash = stats.get("dashboard", {})
            if dash_data['views'] == '0' and old_dash.get('views', '0') != '0':
                for k in ('views', 'earnings', 'ecpm', 'available'):
                    dash_data[k] = old_dash.get(k, dash_data[k])
            record_dashboard_stats(dash_data)
            print(f"[+] Dashboard: Views={dash_data['views']}, Earnings={dash_data['earnings']}, Available={dash_data['available']}")
    except Exception as e:
        print(f"[!] Dashboard check error: {e}")

# ------------------------------------------------------------------------------
# 5. DYNAMIC URL GENERATOR & LINK SHORTENER
# ------------------------------------------------------------------------------
def get_dynamic_url(vercel_base_url=None):
    base = (vercel_base_url or DEFAULT_VERCEL_APP).rstrip('/')
    if REQUESTS_AVAILABLE:
        try:
            res = requests.get(f"{base}/api/link", params={'auth': MASTER_PASSWORD}, timeout=8)
            if res.status_code == 200:
                data = res.json()
                if data.get('url'):
                    return data['url']
        except Exception:
            pass
    rand_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    rand_ts = int(time.time() * 1000)
    cat = random.choice(['tech', 'cloud', 'dev', 'ai', 'crypto', 'quantum', 'fintech'])
    return (f"{base}/article?id={rand_id}&cat={cat}&t={rand_ts}"
            f"&auth={urllib.parse.quote(MASTER_PASSWORD)}&ref=standalone")

def shorten_target(target_url, cookies_path=None):
    if not PLAYWRIGHT_AVAILABLE:
        return None
    c_path = cookies_path or SESSION_FILE
    with shortener_lock:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True,
                    args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
                    ignore_default_args=['--enable-automation'])
                context = browser.new_context(proxy=PROXY_CONFIG, user_agent=USER_AGENT,
                    viewport={'width': 1280, 'height': 720})
                context.add_init_script('''
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {} };
                ''')
                if os.path.exists(c_path):
                    with open(c_path) as f:
                        cookie_dict = json.load(f)
                    context.add_cookies([{'name': k, 'value': v, 'domain': '.ouo.io', 'path': '/'}
                                          for k, v in cookie_dict.items()])
                page = context.new_page()
                page.goto('https://ouo.io/manage/home', wait_until='domcontentloaded', timeout=30000)
                for _ in range(15):
                    time.sleep(1)
                    if 'Just a moment' not in page.title() and page.title() != '':
                        break
                token_el = page.query_selector('input[name="_token"]')
                csrf_token = token_el.get_attribute('value') if token_el else ""
                result = page.evaluate('''async ({url, token}) => {
                    const formData = new URLSearchParams();
                    formData.append('_token', token);
                    formData.append('url', url);
                    const res = await fetch('https://ouo.io/shorten', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest',
                            'Accept': 'application/json, text/javascript, */*; q=0.01'
                        },
                        body: formData.toString()
                    });
                    const raw = await res.text();
                    let data = null;
                    try { data = JSON.parse(raw); } catch(e) { data = raw; }
                    return { status: res.status, data };
                }''', {'url': target_url, 'token': csrf_token})
                browser.close()
                status = result.get('status')
                data   = result.get('data')
                if status == 200 and isinstance(data, dict) and not data.get('error'):
                    return f"https://ouo.io/{data.get('slug')}"
                return None
        except Exception as e:
            print(f"[!] Shorten error: {e}")
            return None

# ------------------------------------------------------------------------------
# 6. VISITOR ENGINE
# ------------------------------------------------------------------------------
def visit_shortlink(short_url, worker_id=1, proxy=None):
    if not PLAYWRIGHT_AVAILABLE:
        return False, "Playwright not installed"
    use_proxy = proxy or VISITOR_PROXY_CONFIG
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled',
                      '--disable-dev-shm-usage', '--disable-gpu', '--window-size=1920,1080'],
                ignore_default_args=['--enable-automation'])
            context = browser.new_context(proxy=use_proxy, user_agent=USER_AGENT,
                viewport={'width': 1920, 'height': 1080})
            context.add_init_script('''
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            ''')
            page = context.new_page()
            page.on("popup", lambda pop: pop.close() if pop else None)

            def bw_filter(route):
                try:
                    req = route.request
                    if 'ouo.io' in req.url or 'ouo.press' in req.url or 'cloudflare.com' in req.url:
                        return route.continue_()
                    if req.resource_type in ["image", "media", "font"]:
                        return route.abort()
                    url_l = req.url.lower()
                    blocked = ["doubleclick", "googlesyndication", "popads", "adcash",
                               "exoclick", "propellerads", "adsterra", "trafficjunky",
                               "adnxs", "creativecdn", "adservice", "analytics",
                               ".mp4", ".webm", "video", "vast", "banner", "cpmstar"]
                    if any(h in url_l for h in blocked):
                        return route.abort()
                    return route.continue_()
                except Exception:
                    pass
            try:
                page.route("**/*", bw_filter)
            except Exception:
                pass

            try:
                page.goto(short_url, wait_until='domcontentloaded', timeout=25000)
            except Exception:
                page.goto(short_url, wait_until='commit', timeout=25000)

            for _ in range(15):
                if 'Just a moment' not in page.title() and page.title() != '':
                    break
                time.sleep(1)

            try:
                page.mouse.wheel(0, random.randint(200, 450))
                time.sleep(random.uniform(0.8, 1.6))
            except Exception:
                pass

            for _ in range(3):
                if '/go/' in page.url or ('ouo.io' not in page.url and 'ouo.press' not in page.url):
                    break
                try:
                    btn1 = page.wait_for_selector('#btn-main', timeout=6000)
                    if btn1:
                        time.sleep(random.uniform(0.6, 1.2))
                        btn1.click(force=True, timeout=4000)
                except Exception:
                    pass
                try:
                    page.wait_for_url(lambda u: '/go/' in u or ('ouo.io' not in u and 'ouo.press' not in u), timeout=6000)
                except Exception:
                    try:
                        page.evaluate('() => { const f = document.querySelector("#form-captcha") || document.querySelector("form"); if (f) f.submit(); }')
                        page.wait_for_url(lambda u: '/go/' in u or ('ouo.io' not in u and 'ouo.press' not in u), timeout=5000)
                    except Exception:
                        pass
                page.bring_to_front()

            if '/go/' in page.url:
                for _ in range(15):
                    time.sleep(1)
                    try:
                        if 'ouo.io' not in page.url and 'ouo.press' not in page.url:
                            break
                        btn2 = page.query_selector('#btn-main')
                        if btn2:
                            cls = btn2.get_attribute('class') or ''
                            if 'disabled' not in cls:
                                time.sleep(random.uniform(0.6, 1.2))
                                try:
                                    btn2.click(force=True, timeout=5000)
                                except Exception:
                                    pass
                                break
                    except Exception:
                        continue

            final_url = page.url
            for i in range(15):
                time.sleep(1)
                try:
                    if 'ouo.io' not in page.url and 'ouo.press' not in page.url:
                        final_url = page.url
                        time.sleep(random.uniform(1.2, 2.0))
                        break
                    if i >= 3 and '/go/' in page.url:
                        try:
                            page.evaluate('() => { const f = document.querySelector("#form-go"); if (f) f.submit(); }')
                        except Exception:
                            pass
                except Exception:
                    continue

            try:
                page.unroute("**/*")
            except Exception:
                pass
            browser.close()
            is_valid = ('ouo.io' not in final_url and 'ouo.press' not in final_url
                        and 'chrome-error' not in final_url)
            return is_valid, final_url
    except Exception as e:
        return False, str(e)

# ------------------------------------------------------------------------------
# 7. CAMPAIGN LOOP
# ------------------------------------------------------------------------------
def run_campaign_loop(total_views=1, vercel_url=None):
    print(f"\n🚀 CAMPAIGN START — Goal: {total_views} views\n")
    if not ensure_valid_session():
        print("[-] Session failed. Aborting.")
        return False
    successful = 0
    for cycle in range(1, total_views + 1):
        print(f"\n>>> [CYCLE {cycle}/{total_views}] Generating URL...")
        dynamic_url = get_dynamic_url(vercel_url)
        print(f"[+] Target: {dynamic_url}")
        short_link = shorten_target(dynamic_url)
        if not short_link:
            if not check_session_validity():
                do_login()
                short_link = shorten_target(dynamic_url)
        if not short_link:
            print("[-] Skipping — shorten failed.")
            continue
        print(f"[+] Shortlink: {short_link}")
        record_link_created(short_link, dynamic_url)
        ok, dest = visit_shortlink(short_link)
        record_view_result(ok, short_link, dest)
        if ok:
            successful += 1
            print(f"[🎉 CYCLE {cycle}] Success → {dest}")
        else:
            print(f"[!] Cycle {cycle} failed: {dest}")
        if cycle < total_views:
            w = random.randint(5, 10)
            print(f"[*] Sleeping {w}s...")
            time.sleep(w)
    print(f"\n🏁 CAMPAIGN DONE: {successful}/{total_views} Views Delivered!\n")
    return successful

# ------------------------------------------------------------------------------
# 8. 24/7 MULTI-WORKER DAEMON
# ------------------------------------------------------------------------------
HARDCODED_LINKS = os.environ.get(
    "OUO_HARDCODED_LINKS",
    "https://ouo.io/TixC7D,https://ouo.io/q232K7,https://ouo.io/90phq1"
).split(",")
HARDCODED_LINKS = [l.strip() for l in HARDCODED_LINKS if l.strip()]

def get_shortlink_from_pool():
    return random.choice(HARDCODED_LINKS) if HARDCODED_LINKS else None

def worker_routine(worker_id):
    print(f"[Worker #{worker_id}] Started.")
    while daemon_running:
        try:
            short_url = get_shortlink_from_pool()
            if not short_url:
                time.sleep(4)
                continue
            proxy_cfg, country_label = get_rotating_visitor_proxy()
            ok, dest = visit_shortlink(short_url, worker_id=worker_id, proxy=proxy_cfg)
            record_view_result(ok, short_url, dest, country=f"{country_label} (Rotating Residential)")
            if ok:
                print(f"[Worker #{worker_id}] ✅ View OK ({country_label}) → {short_url}")
            else:
                print(f"[Worker #{worker_id}] ⚠️ Failed → {dest[:45]}...")
            time.sleep(random.uniform(3.0, 10.0))
        except Exception as e:
            print(f"[Worker #{worker_id}] Error: {e}")
            time.sleep(6)

def run_multi_worker_daemon(num_workers=NUM_WORKERS, blocking=True):
    global daemon_running
    daemon_running = True
    print(f"\n🚀 OUO.IO DAEMON — {num_workers} Workers | US Rotating Proxy")
    print(f"🔗 Links: {', '.join(HARDCODED_LINKS)}\n")
    stats = load_stats()
    stats["daemon_status"] = f"🟢 RUNNING 24/7 ({num_workers} US Workers)"
    if not stats.get("daemon_started_at"):
        stats["daemon_started_at"] = time.time()
        stats["daemon_started_str"] = get_current_time_str(stats["daemon_started_at"])
    save_stats(stats)
    try:
        if not check_session_validity():
            do_login()
    except Exception as e:
        print(f"[!] Session init error: {e}")
    try:
        check_ouo_dashboard_hourly()
    except Exception as e:
        print(f"[!] Initial dashboard check error: {e}")

    def hourly_timer():
        while daemon_running:
            time.sleep(60)
            try:
                check_ouo_dashboard_hourly()
            except Exception:
                pass
    threading.Thread(target=hourly_timer, daemon=True).start()

    threads = []
    for w_id in range(1, num_workers + 1):
        t = threading.Thread(target=worker_routine, args=(w_id,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(random.uniform(2.0, 3.5))
    print(f"[+] {num_workers} Workers running 24/7.")

    if blocking:
        def handle_exit(sig, frame):
            global daemon_running
            print("\n[!] Stopping daemon...")
            daemon_running = False
            s = load_stats()
            s["daemon_status"] = "⏸️ STOPPED"
            save_stats(s)
            sys.exit(0)
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
        while daemon_running:
            time.sleep(1)

# ------------------------------------------------------------------------------
# 9. TELEGRAM BOT
# ------------------------------------------------------------------------------
def run_telegram_bot():
    if not TELEBOT_AVAILABLE:
        print("[-] pyTelegramBotAPI not installed.")
        sys.exit(1)
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
    active_status_messages = {}

    def is_authed(chat_id):
        if not os.path.exists(AUTH_FILE):
            return False
        try:
            with open(AUTH_FILE) as f:
                data = json.load(f)
            return (time.time() - data.get(str(chat_id), 0)) < SESSION_LIFETIME
        except Exception:
            return False

    def set_authed(chat_id):
        data = {}
        if os.path.exists(AUTH_FILE):
            try:
                with open(AUTH_FILE) as f:
                    data = json.load(f)
            except Exception:
                pass
        data[str(chat_id)] = time.time()
        os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
        with open(AUTH_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def safe_delete(chat_id, msg_id):
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    def safe_edit(chat_id, msg_id, text, reply_markup=None):
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                   reply_markup=reply_markup, disable_web_page_preview=True)
            return True
        except ApiTelegramException as e:
            return "not modified" in str(e).lower()
        except Exception:
            return False

    def build_card():
        stats = load_stats()
        dash  = stats.get("dashboard", {})
        return (
            "📊 <b>ROX AI — 24/7 Earning & Automation Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Engine:</b> <b>{stats.get('daemon_status')}</b>\n"
            f"🔗 <b>Links Generated:</b> <code>{stats.get('total_links_created')}</code>\n"
            f"✅ <b>Views Success:</b> <code>{stats.get('total_views_completed')}</code>\n"
            f"⚠️ <b>Failed:</b> <code>{stats.get('failed_views')}</code>\n"
            f"🎯 <b>Success Rate:</b> <b>{stats.get('success_rate')}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💰 <b>ouo.io Stats:</b>\n"
            f"  • 👁️ <b>Views:</b> <b>{dash.get('views','0')}</b>\n"
            f"  • 💵 <b>Earnings:</b> <b>{dash.get('earnings','$0.00')}</b>\n"
            f"  • 📈 <b>eCPM:</b> <b>{dash.get('ecpm','$0.00')}</b>\n"
            f"  • 💳 <b>Available:</b> <b>{dash.get('available','$0.00')}</b>\n"
            f"  • ⏱️ <b>Last Check:</b> <code>{dash.get('last_checked_str','Pending')}</code>\n"
            f"  • ⏳ <b>Next Check:</b> <code>{dash.get('next_check_str','In 1 Hour')}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>Portal:</b> <a href=\"{DEFAULT_VERCEL_APP}\">ouo-portal.vercel.app</a>\n"
            f"🔗 <b>Last Link:</b> <code>{stats.get('last_shortlink','None yet')}</code>\n"
            f"🕒 <b>Last Activity:</b> <code>{stats.get('last_activity_str','None yet')}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>24/7 background earning active.</i>"
        )

    def get_markup():
        m = types.InlineKeyboardMarkup(row_width=2)
        m.row(
            types.InlineKeyboardButton("🔄 Refresh", callback_data="cb_refresh"),
            types.InlineKeyboardButton("🌐 Portal",  url=DEFAULT_VERCEL_APP)
        )
        m.row(
            types.InlineKeyboardButton("💰 Sync Earnings", callback_data="cb_force"),
            types.InlineKeyboardButton("🔑 Session",       callback_data="cb_health")
        )
        return m

    def send_card(chat_id, edit_msg_id=None):
        text   = build_card()
        markup = get_markup()
        if edit_msg_id and safe_edit(chat_id, edit_msg_id, text, reply_markup=markup):
            return
        prev = active_status_messages.get(chat_id)
        if prev and safe_edit(chat_id, prev, text, reply_markup=markup):
            return
        sent = bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        active_status_messages[chat_id] = sent.message_id

    def ask_password(chat_id, del_msg_id=None):
        if del_msg_id:
            safe_delete(chat_id, del_msg_id)
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cb_cancel"))
        msg = bot.send_message(chat_id,
            "🔒 <b>Enter Master Password:</b>", reply_markup=m)
        bot.register_next_step_handler(msg, process_password)

    def process_password(message):
        chat_id = message.chat.id
        safe_delete(chat_id, message.message_id)
        if (message.text or "").strip() == MASTER_PASSWORD:
            set_authed(chat_id)
            send_card(chat_id)
        else:
            m = types.InlineKeyboardMarkup()
            m.add(types.InlineKeyboardButton("🔄 Try Again", callback_data="cb_reauth"))
            bot.send_message(chat_id, "❌ <b>Wrong Password!</b>", reply_markup=m)

    @bot.message_handler(commands=['start', 'status', 'help'])
    def cmd_start(message):
        safe_delete(message.chat.id, message.message_id)
        if not is_authed(message.chat.id):
            ask_password(message.chat.id)
        else:
            send_card(message.chat.id)

    @bot.message_handler(func=lambda m: True)
    def any_msg(message):
        safe_delete(message.chat.id, message.message_id)
        if not is_authed(message.chat.id):
            ask_password(message.chat.id)
        else:
            send_card(message.chat.id)

    @bot.callback_query_handler(func=lambda c: True)
    def callbacks(call):
        chat_id = call.message.chat.id
        if call.data == "cb_cancel":
            bot.clear_step_handler_by_chat_id(chat_id)
            safe_edit(chat_id, call.message.message_id, "❌ Cancelled.")
            bot.answer_callback_query(call.id)
            return
        if call.data == "cb_reauth":
            bot.answer_callback_query(call.id)
            ask_password(chat_id, del_msg_id=call.message.message_id)
            return
        if not is_authed(chat_id):
            bot.answer_callback_query(call.id, "Password required!")
            ask_password(chat_id)
            return
        if call.data == "cb_refresh":
            bot.answer_callback_query(call.id, "Refreshing...")
            threading.Thread(target=lambda: (
                check_ouo_dashboard_hourly(),
                send_card(chat_id, edit_msg_id=call.message.message_id)
            ), daemon=True).start()
        elif call.data == "cb_force":
            bot.answer_callback_query(call.id, "Syncing...")
            threading.Thread(target=lambda: (
                check_ouo_dashboard_hourly(force=True),
                send_card(chat_id, edit_msg_id=call.message.message_id)
            ), daemon=True).start()
        elif call.data == "cb_health":
            bot.answer_callback_query(call.id, "Session checked!")
            send_card(chat_id, edit_msg_id=call.message.message_id)

    def hourly_report():
        while True:
            time.sleep(3600)
            try:
                check_ouo_dashboard_hourly(force=True)
                text   = "🔔 <b>Hourly Auto Report</b>\n" + build_card()
                markup = get_markup()
                for tid in {str(ADMIN_CHAT_ID), "8321630022"}:
                    try:
                        bot.send_message(tid, text, reply_markup=markup, disable_web_page_preview=True)
                    except Exception as err:
                        print(f"[!] Report error to {tid}: {err}")
            except Exception as e:
                print(f"[!] Hourly report error: {e}")
    threading.Thread(target=hourly_report, daemon=True).start()

    print(f"[*] Starting Telegram Bot...")
    try:
        me = bot.get_me()
        print(f"[+] Bot Online: @{me.username}")
    except Exception as e:
        print(f"[!] Bot connect warning: {e}")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e:
            print(f"[!] Polling error: {e}. Retrying in 5s...")
            time.sleep(5)

# ------------------------------------------------------------------------------
# 10. CLI ENTRYPOINT
# ------------------------------------------------------------------------------
def print_status_terminal():
    stats = load_stats()
    dash  = stats.get("dashboard", {})
    print("\n" + "="*50)
    print("📊 OUO AUTOMATION STATS")
    print("="*50)
    print(f"Status:          {stats.get('daemon_status')}")
    print(f"Links:           {stats.get('total_links_created')}")
    print(f"Views OK:        {stats.get('total_views_completed')}")
    print(f"Views Failed:    {stats.get('failed_views')}")
    print(f"Success Rate:    {stats.get('success_rate')}")
    print(f"Last Link:       {stats.get('last_shortlink')}")
    print(f"Last Activity:   {stats.get('last_activity_str')}")
    print("-"*50)
    print(f"Dashboard Views: {dash.get('views')}")
    print(f"Earnings:        {dash.get('earnings')}")
    print(f"eCPM:            {dash.get('ecpm')}")
    print(f"Available:       {dash.get('available')}")
    print(f"Last Checked:    {dash.get('last_checked_str')}")
    print("="*50 + "\n")

def main():
    parser = argparse.ArgumentParser(
        description="ouo.io 24/7 Automation & Telegram Control Center (Standalone Engine)")
    sub = parser.add_subparsers(dest="command")

    p_daemon = sub.add_parser("daemon")
    p_daemon.add_argument("-w", "--workers", type=int, default=NUM_WORKERS)

    sub.add_parser("bot")

    p_all = sub.add_parser("all")
    p_all.add_argument("-w", "--workers", type=int, default=NUM_WORKERS)

    p_camp = sub.add_parser("campaign")
    p_camp.add_argument("views", type=int, nargs="?", default=1)
    p_camp.add_argument("--url", type=str, default=None)

    sub.add_parser("login")

    p_shorten = sub.add_parser("shorten")
    p_shorten.add_argument("url", type=str, nargs="?", default="https://ouo-portal.vercel.app")

    p_visit = sub.add_parser("visit")
    p_visit.add_argument("url", type=str)

    sub.add_parser("status")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "daemon":
        run_multi_worker_daemon(num_workers=args.workers, blocking=True)
    elif args.command == "bot":
        run_telegram_bot()
    elif args.command == "all":
        threading.Thread(
            target=run_multi_worker_daemon,
            kwargs={"num_workers": args.workers, "blocking": False},
            daemon=True
        ).start()
        run_telegram_bot()
    elif args.command == "campaign":
        run_campaign_loop(total_views=args.views, vercel_url=args.url)
    elif args.command == "login":
        print("[+] Login:", "Success" if do_login() else "Failed")
    elif args.command == "shorten":
        print(f"[+] Result: {shorten_target(args.url)}")
    elif args.command == "visit":
        ok, dest = visit_shortlink(args.url)
        print(f"[+] Visit: {'Success' if ok else 'Failed'} → {dest}")
    elif args.command == "status":
        print_status_terminal()

if __name__ == '__main__':
    main()
