#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Woiden VPS 自动续期（HAX 风格 Cookie 登录版）
- Cookie 登录：requests 探测 + 预热 /login + page.set_cookies + JS 兜底
- 按账号索引匹配 SESSION_STRING
- 历史消息 + 轮询后备
- 登录后检测到期时间，已续期则跳过（阈值 24h）
- 每个账号完成后 TG 通知剩余未完成列表
- 未完成账号循环重试，最多 5 轮
- 提交使用 requests 直接 POST /renew-vps-verification/
"""
import os
import sys
import time
import re
import json
import random
import socket
import tempfile
import traceback
import asyncio
from datetime import datetime, timezone, timedelta

import requests as req_lib
from ruyipage import launch, Keys
from PIL import Image, ImageDraw, ImageFont
import urllib.request

# ========== 环境变量 ==========
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON", "[]")
ACCOUNTS = json.loads(ACCOUNTS_JSON)
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
PROXY_SERVER = os.getenv("PROXY_SERVER", "")
DEBUG = os.getenv("DEBUG", "true").lower() == "true"
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
SKIP_THRESHOLD_HOURS = float(os.getenv("SKIP_THRESHOLD_HOURS", "24"))
NOTIFY_PROGRESS = os.getenv("NOTIFY_PROGRESS", "true").lower() == "true"
MAX_RENEW_ROUNDS = int(os.getenv("MAX_RENEW_ROUNDS", "5"))

SESSION_STRINGS = [
    os.getenv("SESSION_STRING_1", ""),
    os.getenv("SESSION_STRING_2", ""),
    os.getenv("SESSION_STRING_3", ""),
    os.getenv("SESSION_STRING_4", ""),
    os.getenv("SESSION_STRING_5", "")
]
SESSION_STRINGS = [s for s in SESSION_STRINGS if s]

TARGET_URL = "https://woiden.id/login"
RENEW_CODE_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')

# Cookie 注入时需要忽略的埋点 cookie
IGNORE_COOKIE_NAMES = {
    "_ga", "_gid", "_gat_gtag_UA_", "__gads", "__gpi", "__eoi",
    "FCCDCF", "FCNEC", "FCOEC",
}


def debug_print(*args, **kwargs):
    if DEBUG:
        print("[DEBUG]", *args, flush=True, **kwargs)


# ========== 代理检测 ==========
def is_port_open(host, port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def get_proxies():
    if not PROXY_SERVER:
        return None
    if PROXY_SERVER.startswith(('http://', 'https://')):
        return {"http": PROXY_SERVER, "https": PROXY_SERVER}
    try:
        if is_port_open('127.0.0.1', 1080) or is_port_open('127.0.0.1', 1081):
            return {"http": PROXY_SERVER, "https": PROXY_SERVER}
    except Exception:
        pass
    return None


def check_proxy_ip(proxies):
    if not proxies:
        return False, None
    services = [
        'https://api.ipify.org?format=json',
        'https://ip.sb/json',
        'https://httpbin.org/ip'
    ]
    for url in services:
        try:
            resp = req_lib.get(url, proxies=proxies, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                ip = data.get('ip') or data.get('origin')
                if ip:
                    return True, ip
        except Exception:
            continue
    return False, None


# ========== 工具函数 ==========
def get_beijing_time():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def send_telegram_message(text, bot_token, chat_id):
    if not bot_token or not chat_id:
        return False
    proxies = get_proxies()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = req_lib.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                            timeout=10, proxies=proxies)
        return resp.json().get("ok", False)
    except Exception:
        try:
            resp = req_lib.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                                timeout=10)
            return resp.json().get("ok", False)
        except Exception:
            return False


def send_telegram_photo(photo_path, caption, bot_token, chat_id):
    if not bot_token or not chat_id or not os.path.exists(photo_path):
        return False
    proxies = get_proxies()
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': chat_id, 'caption': caption}
            resp = req_lib.post(url, data=data, files=files, timeout=30, proxies=proxies)
            return resp.json().get("ok", False)
    except Exception as e:
        print(f"  [TG] 发送图片失败: {e}", flush=True)
        return False


def notify_renewal_success(phone, expiry_date, bot_token, chat_id):
    msg = f"✅ <b>VPS 续期成功</b>\n\nWoiden\n📱 {phone}\n📅 {expiry_date or '未知'}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)


def notify_renewal_failed(phone, step, error, bot_token, chat_id):
    msg = f"❌ <b>VPS 续期失败</b>\n\nWoiden\n📱 {phone}\n📍 {step}\n⚠️ {error}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)


def notify_renewal_skipped(phone, valid_until, remaining_hours, bot_token, chat_id):
    rh_str = f"{remaining_hours:.1f} 小时" if isinstance(remaining_hours, (int, float)) else "未知"
    msg = (f"⏭️ <b>VPS 已续期，跳过</b>\n\nWoiden\n📱 {phone}\n"
           f"📅 到期: {valid_until or '未知'}\n"
           f"⏰ 剩余: {rh_str}\n"
           f"🕒 {get_beijing_time()}")
    send_telegram_message(msg, bot_token, chat_id)


def notify_round_start(round_no, max_rounds, pending_accounts, bot_token, chat_id):
    if not bot_token or not chat_id:
        return
    lines = [f"🔄 <b>Woiden 第 {round_no}/{max_rounds} 轮开始</b>", ""]
    lines.append(f"⏳ <b>待处理 ({len(pending_accounts)})：</b>")
    for i, a in enumerate(pending_accounts, 1):
        lines.append(f"  {i}. <code>{a.get('phone', '?')}</code>")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)


def notify_round_end(round_no, will_retry, pending_accounts, bot_token, chat_id):
    if not bot_token or not chat_id:
        return
    lines = [f"📋 <b>Woiden 第 {round_no} 轮结束</b>", ""]
    if will_retry:
        lines.append(f"⏳ 仍有 {len(pending_accounts)} 个账号未完成，将进入下一轮重试：")
        for i, a in enumerate(pending_accounts, 1):
            lines.append(f"  {i}. <code>{a.get('phone', '?')}</code>")
    else:
        lines.append("🎉 本轮全部处理完毕")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)


def notify_progress(current_idx, total, phone, status_emoji, status_text,
                    pending_list, bot_token, chat_id):
    if not bot_token or not chat_id:
        return
    lines = [f"{status_emoji} <b>Woiden 进度 {current_idx}/{total}</b>",
             "",
             f"📱 刚完成: <code>{phone}</code>",
             f"📌 结果: {status_text}",
             ""]
    if pending_list:
        lines.append(f"⏳ <b>未完成 ({len(pending_list)})：</b>")
        for i, p in enumerate(pending_list, 1):
            lines.append(f"  {i}. <code>{p}</code>")
    else:
        lines.append("🎉 <b>所有账号已处理完毕</b>")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)


def notify_all_done(total, success, failed, skipped, failed_list, bot_token, chat_id):
    if not bot_token or not chat_id:
        return
    lines = [
        "🎊 <b>今日 woiden 续期全部完成</b>",
        "",
        f"📊 总数: {total}",
        f"✅ 成功: {success}",
        f"⏭️ 跳过: {skipped}",
        f"❌ 失败: {failed}",
    ]
    if failed_list:
        lines.append("")
        lines.append("⚠️ <b>失败账号：</b>")
        for i, p in enumerate(failed_list, 1):
            lines.append(f"  {i}. <code>{p}</code>")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)


def take_screenshot(page, path, bot_token, chat_id, caption):
    """DrissionPage / ruyipage 兼容截图"""
    try:
        if hasattr(page, 'get_screenshot'):
            try:
                page.get_screenshot(path=path, full_page=True)
            except TypeError:
                page.get_screenshot(path)
        elif hasattr(page, 'screenshot'):
            page.screenshot(path)
        else:
            # 兜底：底层 driver
            driver = getattr(page, 'driver', None) or getattr(page, '_driver', None)
            if driver and hasattr(driver, 'get_screenshot_as_file'):
                driver.get_screenshot_as_file(path)
            else:
                raise RuntimeError("无可用截图方法")
        if os.path.exists(path):
            send_telegram_photo(path, caption, bot_token, chat_id)
    except Exception as e:
        print(f"  [截图] 失败: {e}", flush=True)


# ========== 到期时间检测 ==========
def get_page_field_value(page, label_text):
    try:
        js = """
        (function(lbl) {
            var labels = document.querySelectorAll('label.col-form-label, label');
            for (var i = 0; i < labels.length; i++) {
                var t = (labels[i].textContent || '').trim();
                if (t.toLowerCase() === lbl.toLowerCase()) {
                    var parent = labels[i].closest('.row') || labels[i].parentElement;
                    if (parent) {
                        var valDiv = parent.querySelector('.col-sm-7, .col-sm-6, .col-md-7, div');
                        if (valDiv && valDiv !== labels[i]) {
                            return (valDiv.textContent || '').trim();
                        }
                    }
                }
            }
            return '';
        })('%s');
        """ % label_text.replace("'", "\\'")
        return page.run_js(js) or ""
    except Exception as e:
        debug_print(f"get_page_field_value({label_text}) 异常: {e}")
        return ""


def parse_dt(s):
    if not s:
        return None
    s = re.sub(r'\(.*?\)', '', s).strip()
    s = re.sub(r'\s+', ' ', s)

    MONTHS = {
        'january': 1, 'jan': 1, 'february': 2, 'feb': 2,
        'march': 3, 'mar': 3, 'april': 4, 'apr': 4, 'may': 5,
        'june': 6, 'jun': 6, 'july': 7, 'jul': 7, 'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9, 'october': 10, 'oct': 10,
        'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
    }

    def month_num(name):
        if not name:
            return None
        return MONTHS.get(name.strip().lower().rstrip('.,'))

    m = re.match(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*-\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$', s)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2))
        ss = int(m.group(3)) if m.group(3) else 0
        mon = month_num(m.group(4))
        day = int(m.group(5)); year = int(m.group(6))
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    m = re.match(r'^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$', s)
    if m:
        mon = month_num(m.group(1))
        day = int(m.group(2)); year = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    m = re.match(r'^(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$', s)
    if m:
        day = int(m.group(1))
        mon = month_num(m.group(2))
        year = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def check_should_renew(page):
    valid_str = get_page_field_value(page, "Valid until")
    current_str = get_page_field_value(page, "Current time")

    if not valid_str:
        debug_print("未找到 'Valid until' 字段，继续续期流程")
        return True, None, None

    valid_dt = parse_dt(valid_str)
    if not valid_dt:
        print(f"  [CHECK] ⚠️ 无法解析到期时间: {valid_str!r}，继续续期")
        return True, valid_str, None

    now_dt = parse_dt(current_str) or datetime.now()
    remaining_hours = (valid_dt - now_dt).total_seconds() / 3600.0

    print(f"  [CHECK] Valid until : {valid_str}")
    print(f"  [CHECK] Current time: {now_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  [CHECK] 剩余: {remaining_hours:.2f} 小时 (阈值 {SKIP_THRESHOLD_HOURS} 小时)")

    if remaining_hours > SKIP_THRESHOLD_HOURS:
        return False, valid_str, remaining_hours
    return True, valid_str, remaining_hours


# ========== 续期码文件读写 ==========
def read_code_from_file(code_file, consume=False):
    try:
        if os.path.exists(code_file):
            with open(code_file, 'r', encoding='utf-8') as f:
                code = f.read().strip()
            if code and RENEW_CODE_PATTERN.search(code):
                print(f"  [文件] ✅ 从 {code_file} 读取到续期码: {code[:20]}...")
                if consume:
                    try:
                        os.remove(code_file)
                    except Exception:
                        pass
                return code
    except Exception as e:
        print(f"  [文件] 读取异常: {e}", flush=True)
    return None


def write_code_to_file(code_file, code):
    try:
        with open(code_file, 'w', encoding='utf-8') as f:
            f.write(code)
        os.chmod(code_file, 0o600)
        print(f"  [文件] ✅ 续期码已写入 {code_file}: {code[:20]}...")
        return True
    except Exception as e:
        print(f"  [文件] 写入失败: {e}", flush=True)
        return False


# ========== 从聊天历史获取续期码 ==========
async def get_code_from_chat_history_async(session_string, api_id, api_hash, bot_username='HaxTG_bot'):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    try:
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.start()
        entity = await client.get_entity(bot_username)
        messages = await client.get_messages(entity, limit=10)
        for msg in messages:
            if msg.text:
                match = RENEW_CODE_PATTERN.search(msg.text)
                if match:
                    code = match.group(0)
                    print(f"  [历史] ✅ 从聊天记录提取到续期码: {code[:20]}...")
                    await client.disconnect()
                    return code
        await client.disconnect()
        return None
    except Exception as e:
        print(f"  [历史] 查询失败: {e}")
        return None


def get_code_from_history(session_string):
    if not session_string or not API_ID or not API_HASH:
        return None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        code = loop.run_until_complete(
            get_code_from_chat_history_async(session_string, API_ID, API_HASH)
        )
        loop.close()
        return code
    except Exception as e:
        print(f"  [历史] 异步执行失败: {e}")
        return None


# ========== 轮询 Telegram API ==========
def get_renewal_code_from_telegram(bot_tokens, code_file, timeout=600, poll_interval=5):
    if not bot_tokens:
        print("  [CODE] ⚠️ bot_tokens 为空，无法轮询")
        return "", None

    print(f"  [CODE] 开始轮询，共 {len(bot_tokens)} 个 Bot Token")
    offsets = {}
    for bt in bot_tokens:
        try:
            proxies = get_proxies()
            url = f"https://api.telegram.org/bot{bt['token']}/getUpdates"
            resp = req_lib.get(url, timeout=10, proxies=proxies) if proxies else req_lib.get(url, timeout=10)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                # 先尝试从已有历史里抢救一条码，再更新 offset
                for u in data["result"]:
                    txt = (u.get("message", {}) or {}).get("text", "") or \
                          (u.get("message", {}) or {}).get("caption", "")
                    m = RENEW_CODE_PATTERN.search(txt or "")
                    if m:
                        code = m.group(0)
                        write_code_to_file(code_file, code)
                        return code, bt.get("label", bt['token'][-6:])
                offsets[bt['token']] = max(u["update_id"] for u in data["result"]) + 1
            else:
                offsets[bt['token']] = 0
        except Exception as e:
            print(f"  [CODE] 获取偏移量失败 {bt['token'][-6:]}: {e}")
            offsets[bt['token']] = 0

    elapsed = 0
    while elapsed < timeout:
        file_code = read_code_from_file(code_file, consume=False)
        if file_code:
            print(f"  [CODE] 从文件 {code_file} 读取到续期码，直接使用", flush=True)
            return file_code, "file"

        for bt in bot_tokens:
            offset = offsets.get(bt['token'], 0)
            try:
                proxies = get_proxies()
                url = f"https://api.telegram.org/bot{bt['token']}/getUpdates?offset={offset}&timeout=5"
                resp = (req_lib.get(url, timeout=10, proxies=proxies) if proxies else req_lib.get(url, timeout=10))
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offsets[bt['token']] = update["update_id"] + 1
                        msg = update.get("message", {})
                        text = msg.get("text", "") or msg.get("caption", "")
                        if text:
                            match = RENEW_CODE_PATTERN.search(text)
                            if match:
                                code = match.group(0)
                                write_code_to_file(code_file, code)
                                return code, bt.get("label", bt['token'][-6:])
            except Exception as e:
                print(f"  [CODE] 轮询异常 {bt['token'][-6:]}: {e}")

        time.sleep(poll_interval)
        elapsed += poll_interval
        if elapsed % 60 < poll_interval:
            print(f"  [CODE] 等待中... ({elapsed//60} 分钟)", flush=True)

    return "", None


# ========== Cookie 规范化 & 探测 & 注入（HAX 风格）==========
def normalize_cookies(cookies_data, default_domain=".woiden.id"):
    """把 cookie 输入规范化为 dict 列表（支持字符串 / 列表 / None）"""
    if isinstance(cookies_data, str):
        # 允许只传一个 PHPSESSID 值
        return [
            {"name": "PHPSESSID", "value": cookies_data,
             "domain": default_domain, "path": "/"},
            {"name": "PHPSESSID", "value": cookies_data,
             "domain": default_domain, "path": "/vps-info"},
        ]

    if not isinstance(cookies_data, list):
        return []

    result = []
    for c in cookies_data:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = str(c.get("domain", default_domain))
        if "woiden.id" not in domain:
            continue
        if any(name == ig or name.startswith(ig) for ig in IGNORE_COOKIE_NAMES):
            continue

        nc = {
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": str(c.get("path", "/")),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp:
            try:
                nc["expires"] = int(float(exp))
            except Exception:
                pass
        result.append(nc)

    seen, uniq = set(), []
    for c in result:
        key = (c["name"], c["domain"], c["path"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def probe_cookie_with_requests(sess_value, proxies=None):
    """用 requests 直接带 PHPSESSID 访问 /vps-info，探测服务端是否认这个 session"""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://woiden.id/login",
    }
    try:
        r = req_lib.get(
            "https://woiden.id/vps-info",
            cookies={"PHPSESSID": sess_value},
            headers=headers,
            proxies=proxies,
            allow_redirects=True,
            timeout=30,
        )
    except Exception as e:
        return False, f"requests 异常: {e}"

    text = r.text or ""
    text_lower = text.lower()

    print(f"    [探测] HTTP {r.status_code}, final={r.url}, len={len(text)}", flush=True)
    if DEBUG:
        print(f"    [探测] 响应头: {dict(r.headers)}", flush=True)
        print(f"    [探测] 响应体前 500 字符:\n{text[:500]}", flush=True)

    is_cf_title = "<title>just a moment" in text_lower
    is_cf_short = len(text) < 5000 and "challenge-platform" in text_lower
    is_cf = is_cf_title or is_cf_short

    has_logout = ("Logout" in text) or ("Log out" in text) or ("logout" in text)
    has_valid_until = "Valid until" in text
    has_vps_info = "VPS Information" in text or "vps-info" in (r.url or "")

    if has_logout or has_valid_until or has_vps_info:
        return True, f"服务端有效 (HTTP {r.status_code})"
    if is_cf:
        return False, f"Cloudflare 拦截 (HTTP {r.status_code})"
    return False, f"状态不明 (HTTP {r.status_code}, len={len(text)})"


def set_session_cookie(page, cookies_data, proxies=None):
    """
    HAX 风格 Cookie 注入：
      1. requests 探测服务端
      2. 访问 /login 预热
      3. page.set_cookies 注入
      4. JS document.cookie 兜底
    """
    cookies_list = normalize_cookies(cookies_data, default_domain=".woiden.id")
    if not cookies_list:
        print("  [COOKIE] ⚠️ cookie 数据为空或格式不支持", flush=True)
        return False

    sess_value = next((c["value"] for c in cookies_list if c["name"] == "PHPSESSID"), None)
    if not sess_value:
        print("  [COOKIE] ⚠️ 没有 PHPSESSID，无法注入", flush=True)
        return False

    print(f"  [COOKIE] 目标 PHPSESSID: {sess_value[:8]}...{sess_value[-4:]}", flush=True)

    # ---- 0. requests 探测（不影响后续浏览器注入）----
    ok, info = probe_cookie_with_requests(sess_value, proxies)
    if ok:
        print(f"  [COOKIE] ✅ requests 探测：{info}", flush=True)
    else:
        print(f"  [COOKIE] ⚠️ requests 探测：{info}（继续尝试浏览器注入）", flush=True)

    # ---- 1. 访问 /login 预热 ----
    try:
        page.get("https://woiden.id/login")
        page.wait.doc_loaded(timeout=20)
        time.sleep(2)
        print(f"  [COOKIE] 当前页面: {page.url}", flush=True)
    except Exception as e:
        debug_print(f"预访问 /login 失败: {e}")

    # ---- 2. page.set_cookies ----
    try:
        page.set_cookies(cookies_list)
        print(f"  [COOKIE] ✅ page.set_cookies 调用成功（{len(cookies_list)} 条）", flush=True)
    except Exception as e:
        print(f"  [COOKIE] ❌ page.set_cookies 失败: {e}", flush=True)
        return False

    # ---- 3. JS 兜底（带 domain / 不带 domain 各写一遍）----
    try:
        page.run_js(
            f"document.cookie = 'PHPSESSID={sess_value}; path=/; domain=.woiden.id; SameSite=Lax';"
        )
        page.run_js(
            f"document.cookie = 'PHPSESSID={sess_value}; path=/; SameSite=Lax';"
        )
        print("  [COOKIE] ✅ JS 注入完成", flush=True)
    except Exception as e:
        print(f"  [COOKIE] ⚠️ JS 注入失败: {e}", flush=True)

    return True


# ========== 页面操作函数 ==========
def is_logged_in(page):
    try:
        url = page.url or ""
        # 被重定向回 /login 就一定未登录
        if "/login" in url and "vps-info" not in url:
            return False

        logout_btn = page.ele(
            "xpath://*[contains(text(), 'Logout') or contains(text(), 'Log out')]",
            timeout=2,
        )
        if logout_btn and logout_btn.is_displayed:
            return True

        if "woiden.id/vps-info" in url:
            menu = page.ele("css:a.nav-link.dropdown-toggle", timeout=2)
            if menu and menu.is_displayed:
                return True
        return False
    except Exception:
        return False


# ---- 算术验证码 ----
def _digit_to_grid(img_path, gw=12, gh=18):
    img = Image.open(img_path).convert('RGB')
    px = img.load()
    w, h = img.size
    blue = [(x, y) for y in range(h) for x in range(w)
            if px[x, y][2] > 150 and px[x, y][0] < 100]
    if not blue:
        return None
    mx0 = min(p[0] for p in blue)
    mx1 = max(p[0] for p in blue)
    my0 = min(p[1] for p in blue)
    my1 = max(p[1] for p in blue)
    bw = mx1 - mx0 + 1
    bh = my1 - my0 + 1
    grid = [[0] * bw for _ in range(bh)]
    for x, y in blue:
        grid[y - my0][x - mx0] = 1
    res = [[0] * gw for _ in range(gh)]
    for ty in range(gh):
        for tx in range(gw):
            sy = ty * bh / gh
            sx = tx * bw / gw
            total, cnt = 0, 0
            for dy in range(2):
                for dx in range(2):
                    yy, xx = int(sy) + dy, int(sx) + dx
                    if 0 <= yy < bh and 0 <= xx < bw:
                        total += grid[yy][xx]
                        cnt += 1
            if cnt > 0 and total / cnt > 0.3:
                res[ty][tx] = 1
    return res


def _render_ref_grid(digit):
    gsize = 24
    img = Image.new('RGB', (gsize, gsize), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = None
    for fn in ['arialbd.ttf', 'arial.ttf', 'segoeuib.ttf', 'segoeui.ttf',
               'calibrib.ttf', 'calibri.ttf', 'timesbd.ttf']:
        try:
            font = ImageFont.truetype(fn, gsize - 4)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), str(digit), font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (gsize - tw) // 2 - bbox[0]
    ty = (gsize - th) // 2 - bbox[1]
    draw.text((tx, ty), str(digit), fill=(0, 0, 255), font=font)
    tmp = f"_tmp_ref_{digit}.png"
    img.save(tmp)
    grid = _digit_to_grid(tmp, 12, 18)
    try:
        os.remove(tmp)
    except Exception:
        pass
    return grid


def _fetch_image_bytes(page, url):
    import base64
    try:
        url_json = json.dumps(url)
        b64 = page.run_js(
            "(async function(){"
            "  try {"
            "    const r = await fetch(" + url_json + ", {credentials:'include', referrerPolicy:'unsafe-url'});"
            "    if (!r.ok) return '';"
            "    const b = await r.blob();"
            "    const buf = await b.arrayBuffer();"
            "    const bytes = new Uint8Array(buf);"
            "    let bin = '';"
            "    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);"
            "    return 'data:' + (b.type || 'image/png') + ';base64,' + btoa(bin);"
            "  } catch(e) { return ''; }"
            "})()"
        )
        if b64 and b64.startswith('data:image'):
            return base64.b64decode(b64.split(',', 1)[1])
    except Exception as e:
        debug_print(f"    图片下载(浏览器)失败: {e}")
    try:
        cookie_header = ""
        try:
            cookies = page.get_cookies()
            cookie_header = "; ".join(
                f"{getattr(c, 'name', None) or c.get('name')}={getattr(c, 'value', None) or c.get('value')}"
                for c in cookies
                if (getattr(c, 'name', None) or (isinstance(c, dict) and c.get('name')))
            )
        except Exception:
            pass
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": page.url or TARGET_URL,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        req = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        return None


def solve_math_captcha(page):
    print("  [CAPTCHA] 识别算式验证码...")
    page.wait(2)
    group_urls = []
    op_text = ""
    group_data = page.run_js("""
        (function() {
            var groups = document.querySelectorAll('.form-group.row');
            for (var g = 0; g < groups.length; g++) {
                var imgs = groups[g].querySelectorAll('img');
                if (imgs.length >= 2) {
                    var urls = [];
                    for (var i = 0; i < imgs.length; i++) urls.push(imgs[i].src || '');
                    var opTxt = '';
                    var walker = document.createTreeWalker(groups[g], NodeFilter.SHOW_TEXT, null, false);
                    while (walker.nextNode()) {
                        var t = walker.currentNode.textContent.trim();
                        if (t.length <= 3 && /[+\\-×÷*/xX]/.test(t)) { opTxt = t; break; }
                    }
                    if (!opTxt) {
                        var els = groups[g].querySelectorAll('*');
                        for (var e = 0; e < els.length; e++) {
                            var t = els[e].textContent.trim();
                            if (t.length <= 3 && /[+\\-×÷*/xX]/.test(t) && els[e].querySelectorAll('img').length === 0) { opTxt = t; break; }
                        }
                    }
                    return JSON.stringify({urls: urls, op: opTxt});
                }
            }
            return JSON.stringify({urls: [], op: ''});
        })();
    """)
    try:
        gd = json.loads(group_data) if isinstance(group_data, str) else {}
        group_urls = [u for u in (gd.get('urls') or []) if u]
        op_text = (gd.get('op') or '').strip()
    except Exception:
        pass
    if len(group_urls) < 2:
        debug_print("  [CAPTCHA] 未从 .form-group.row 获取到图片，扫描全页...")
        all_imgs = page.run_js("""
            var imgs = document.querySelectorAll('img');
            var urls = [];
            for (var i = 0; i < imgs.length; i++) {
                if (imgs[i].src && imgs[i].src.match(/temp|captcha/)) urls.push(imgs[i].src);
            }
            JSON.stringify(urls);
        """)
        try:
            all_urls = json.loads(all_imgs) if all_imgs else []
            group_urls = [u for u in all_urls if 'temp' in u or 'captcha' in u][:2]
        except Exception:
            pass
    if not op_text:
        body_text = page.run_js("document.body.innerText") or ""
        for symbol in ['×', '÷', '+', '-', '*', '/']:
            if symbol in body_text:
                op_text = symbol
                break
    print(f"  [CAPTCHA] 找到 {len(group_urls)} 张图片, 运算符: '{op_text}'")
    for i, url in enumerate(group_urls):
        print(f"    url[{i}]: {url}")
    if len(group_urls) < 2 or not op_text:
        print("  [CAPTCHA] 图片或运算符不足")
        return None

    def digit_from_url(url):
        try:
            m = re.search(r'-(\d)', url or '')
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None

    digits = []
    for url in group_urls[:2]:
        d = digit_from_url(url)
        if d is not None:
            digits.append(d)
            print(f"  图片提取数字: {d}")
        else:
            print(f"  图片未提取到数字: {url}")
            digits.append(0)
    if len(digits) < 2:
        print("  [CAPTCHA] 数字提取失败")
        return None
    op = '+'
    if op_text in ('×', '*', 'x', 'X'):
        op = '*'
    elif op_text in ('−', '-', '－'):
        op = '-'
    result = eval(f"{digits[0]} {op} {digits[1]}")
    op_symbol = '×' if op == '*' else ('−' if op == '-' else '+')
    print(f"  [CAPTCHA] 算式: {digits[0]} {op_symbol} {digits[1]} = {result}")
    return str(result)


# ---- 广告处理 ----
def close_ads(page):
    print("  [AD] 关闭广告...")
    page.wait(3)
    try:
        page.actions.press(Keys.ESCAPE).perform()
        page.wait(1)
    except Exception:
        pass
    for keyword in ["Close", "close", "×", "关闭"]:
        try:
            el = page.ele(f'xpath://*[contains(text(), "{keyword}")]')
            if el and el.is_displayed:
                el.click_self()
                page.wait(1)
                break
        except Exception:
            pass
    page.wait(3)
    js_remove = """
    (function() {
        var resp = document.getElementById('response');
        var selectors = [
            '#vpn-server', 'div.overlay',
            '.modal-backdrop', '.popup-overlay',
            '.ad-container', '.ad-wrapper', '.banner-ad'
        ];
        selectors.forEach(function(sel) {
            document.querySelectorAll(sel).forEach(function(el) {
                if (resp && el.contains(resp)) return;
                try { el.remove(); } catch(e) {}
            });
        });
    })();
    """
    try:
        page.run_js(js_remove)
        page.wait(1)
    except Exception:
        pass


def handle_ad_wall(page):
    print("检查广告墙...")
    page.wait(3)
    ad_btn = None
    selectors = [
        'css:button.fc-list-item-button.fc-rewarded-ad-button',
        'text:View a short ad',
        'css:.fc-rewarded-ad-button',
        'css:button[class*="fc-rewarded"]',
    ]
    for sel in selectors:
        try:
            el = page.ele(sel)
            if el and el.is_displayed:
                ad_btn = el
                break
        except Exception:
            continue
    if not ad_btn:
        print("未找到广告按钮")
        return True
    print("点击广告按钮...")
    try:
        ad_btn.click_self(by_js=True)
    except Exception:
        ad_btn.click_self()
    print("等待广告播放...")
    started = time.time()
    while time.time() - started < 150:
        visible = page.run_js("""
            (function() {
                var sels = '.fc-monetization-dialog, .fc-dialog, .fc-message-root, #goog_fullscreen_ad';
                var els = document.querySelectorAll(sels);
                for (var i = 0; i < els.length; i++) {
                    var s = getComputedStyle(els[i]);
                    if (s.display !== 'none' && s.visibility !== 'hidden' && els[i].offsetWidth > 50) return true;
                }
                return false;
            })();
        """)
        if not visible:
            print("广告已解锁")
            return True
        time.sleep(3)
    print("广告解锁超时，强制继续")
    return True


# ---- 强制输入值 ----
def _hard_set_value(page, value, *selectors):
    sel_json = json.dumps(list(selectors))
    val_js = value.replace("\\", "\\\\").replace("'", "\\'")
    js = """(function(v, sels){
var el=null;
for(var i=0;i<sels.length;i++){try{el=document.querySelector(sels[i]);}catch(e){el=null;}if(el)break;}
if(!el)return JSON.stringify({ok:false,reason:'NO_EL'});
try{el.scrollIntoView({block:'center'});}catch(e){}
try{el.focus();}catch(e){}
try{var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;setter.call(el,v);}catch(e){el.value=v;}
el.dispatchEvent(new Event('input',{bubbles:true}));
el.dispatchEvent(new Event('change',{bubbles:true}));
el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true}));
el.dispatchEvent(new Event('blur',{bubbles:true}));
var afterAll=el.value;
return JSON.stringify({ok:afterAll===v,afterAll:afterAll});
})('%s', %s)""" % (val_js, sel_json)
    try:
        res = page.run_js(js)
    except Exception as e:
        return False, '', 'RUNJS_EXC:%s' % e
    d = None
    if isinstance(res, dict):
        d = res
    else:
        try:
            d = json.loads(res or '{}')
        except Exception:
            return False, '', 'PARSE_FAIL:%r' % res
    ok = bool(d.get('ok'))
    after = d.get('afterAll', '')
    return ok, after, ''


# ========== reCAPTCHA 相关 ==========
def find_frame(page, keyword):
    try:
        frames = page.get_frames()
        for frame in frames:
            frame_url = (frame.url or "").lower()
            if "recaptcha" in frame_url and keyword in frame_url:
                return frame
    except Exception:
        pass
    return None


def is_recaptcha_solved(page):
    try:
        for frame in page.get_frames():
            token = frame.run_js(
                "(() => { try { const el = document.querySelector('textarea[name=g-recaptcha-response]'); return el ? el.value : ''; } catch(e) { return ''; } })()"
            )
            if token and len(token) > 30:
                return True
    except Exception:
        pass
    try:
        token = page.run_js(
            "(() => { try { const el = document.querySelector('textarea[name=g-recaptcha-response]'); return el ? el.value : ''; } catch(e) { return ''; } })()"
        )
        if token and len(token) > 30:
            return True
    except Exception:
        pass
    anchor = find_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js(
                "(() => { try { const el = document.querySelector('#recaptcha-anchor'); return el ? (el.getAttribute('aria-checked') === 'true') : false; } catch(e) { return false; } })()"
            )
            if checked:
                return True
        except Exception:
            pass
    return False


def get_recaptcha_token(page):
    try:
        token = page.run_js(
            "(function(){var el=document.querySelector('textarea[name=\"g-recaptcha-response\"]');return el?el.value:'';})()"
        ) or ""
        if token and len(token) > 30:
            return token
    except Exception:
        pass
    try:
        for frame in page.get_frames():
            token = frame.run_js(
                "(function(){var el=document.querySelector('textarea[name=\"g-recaptcha-response\"]');return el?el.value:'';})()"
            ) or ""
            if token and len(token) > 30:
                return token
    except Exception:
        pass
    return ""


def click_recaptcha_checkbox(page):
    anchor = find_frame(page, "anchor")
    if not anchor:
        for _ in range(120):
            anchor = find_frame(page, "anchor")
            if anchor:
                break
            time.sleep(1)
        if not anchor:
            raise RuntimeError("reCAPTCHA anchor iframe not found")
    checkbox = anchor.ele("#recaptcha-anchor", timeout=3)
    if not checkbox:
        raise RuntimeError("reCAPTCHA checkbox not found")
    page.actions.move_to(checkbox, duration=1)
    time.sleep(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except Exception:
        checkbox.click(by_js=True)
    time.sleep(3)


def switch_to_audio(page):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele("#audio-response", timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    for _ in range(3):
        try:
            audio_btn = bframe.ele("#recaptcha-audio-button", timeout=3)
            if audio_btn:
                try:
                    audio_btn.click()
                except Exception:
                    audio_btn.click(by_js=True)
                time.sleep(3)
                input_box = bframe.ele("#audio-response", timeout=1)
                if input_box and input_box.states.is_displayed:
                    return True
        except Exception:
            pass
    try:
        bframe.run_js(
            "(() => { const btn = document.querySelector('#recaptcha-audio-button'); if (btn) btn.click(); })()"
        )
        time.sleep(3)
        input_box = bframe.ele("#audio-response", timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    return False


def get_audio_url(page):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = bframe.ele(".rc-audiochallenge-tdownload-link", timeout=1)
            if link:
                href = link.attr("href")
                if href and len(href) > 10:
                    return href
            link = bframe.ele(".rc-audiochallenge-ndownload-link", timeout=1)
            if link:
                href = link.attr("href")
                if href and len(href) > 10:
                    return href
            audio = bframe.ele("#audio-source", timeout=1)
            if audio:
                src = audio.attr("src")
                if src and len(src) > 10:
                    return src
        except Exception:
            pass
        time.sleep(1)
    return None


def download_audio(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.google.com/",
    }
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))
    for audio_url in urls:
        try:
            r = req_lib.get(audio_url, headers=headers, timeout=30)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except Exception:
            pass
    return None


def recognize_audio(mp3_path):
    try:
        import speech_recognition as sr
        from pydub import AudioSegment
    except ImportError:
        sr = None
        AudioSegment = None
    if sr and AudioSegment:
        try:
            wav_path = mp3_path.replace(".mp3", ".wav")
            AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
                text = recognizer.recognize_google(audio_data)
            try:
                os.remove(wav_path)
            except Exception:
                pass
            if text:
                print(f"  [STT] Google 识别: {text}", flush=True)
                return text
        except Exception as e:
            print(f"  [STT] Google 失败: {e}", flush=True)
    audio_api_url = os.getenv("AUDIO_API_URL")
    if audio_api_url:
        try:
            with open(mp3_path, "rb") as f:
                files = {"audio": f}
                resp = req_lib.post(audio_api_url, files=files, timeout=30)
                resp.raise_for_status()
                result = resp.json()
                text = result.get("text") or result.get("result") or result.get("data")
                if text:
                    print(f"  [API] 备用识别: {text}", flush=True)
                    return text
        except Exception as e:
            print(f"  [API] 备用识别失败: {e}", flush=True)
    return None


def fill_and_verify(page, text):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele("#audio-response", timeout=2)
        if not input_box:
            return False
        input_box.click()
        input_box.clear()
        input_box.input(text)
    except Exception:
        return False
    time.sleep(random.uniform(0.5, 1.5))
    try:
        verify_btn = bframe.ele("#recaptcha-verify-button", timeout=2)
        if verify_btn:
            try:
                verify_btn.click()
            except Exception:
                verify_btn.click(by_js=True)
    except Exception:
        pass
    return True


def solve_recaptcha(page, timeout=60):
    print("  [reCAPTCHA] 开始处理音频验证...", flush=True)
    start_time = time.time()
    for _ in range(int(timeout / 2)):
        if find_frame(page, "anchor"):
            break
        time.sleep(2)
    while time.time() - start_time < timeout:
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] 已通过！", flush=True)
            return True
        try:
            click_recaptcha_checkbox(page)
        except Exception as e:
            print(f"  [reCAPTCHA] 点击复选框失败: {e}", flush=True)
            time.sleep(2)
            continue
        time.sleep(2)
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] 点击后直接通过！", flush=True)
            return True
        if not switch_to_audio(page):
            time.sleep(2)
            if not switch_to_audio(page):
                print("  [reCAPTCHA] 无法切换到音频模式", flush=True)
                time.sleep(random.uniform(2, 4))
                continue
        time.sleep(random.uniform(2, 4))
        audio_url = get_audio_url(page)
        if not audio_url:
            print("  [reCAPTCHA] 未找到音频 URL，重试...", flush=True)
            time.sleep(random.uniform(3, 6))
            continue
        print(f"  [reCAPTCHA] 音频 URL: {audio_url[:80]}...", flush=True)
        mp3_path = download_audio(audio_url)
        if not mp3_path:
            print("  [reCAPTCHA] 音频下载失败，重试...", flush=True)
            time.sleep(random.uniform(3, 6))
            continue
        print(f"  [reCAPTCHA] 音频已下载: {os.path.basename(mp3_path)}", flush=True)
        text = recognize_audio(mp3_path)
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        if not text:
            print("  [reCAPTCHA] 无法识别语音，重试...", flush=True)
            time.sleep(random.uniform(3, 6))
            continue
        print(f"  [reCAPTCHA] 识别结果: [{text}]", flush=True)
        fill_and_verify(page, text)
        time.sleep(5)
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] 语音验证通过！", flush=True)
            return True
        else:
            print("  [reCAPTCHA] 验证未通过，重新获取音频...", flush=True)
            time.sleep(random.uniform(2, 4))
    print(f"  [reCAPTCHA] {timeout} 秒超时", flush=True)
    return False


# ========== 单账号续期主流程 ==========
def renew_account(account, account_index=1):
    phone = account["phone"]
    session_token = account.get("session_token", "")
    code_file = account.get("code_file", "renewal_code.txt")
    bot_token = account.get("bot_token", "")
    chat_id = account.get("chat_id", "")

    print(f"\n{'='*60}\n  续期: {phone}\n{'='*60}", flush=True)

    proxies = get_proxies()
    if proxies:
        print(f"🔗 代理地址: {PROXY_SERVER}")
        ok, ip = check_proxy_ip(proxies)
        if ok:
            print(f"📍 代理出口 IP: {ip}")
        else:
            print("⚠️ 代理出口 IP 获取失败，将使用直连")
            proxies = None
    else:
        print("🔗 代理不可用，使用直连")

    page = None
    try:
        launch_args = {"headless": HEADLESS, "window_size": (1366, 768)}
        if proxies and PROXY_SERVER:
            launch_args["proxy"] = PROXY_SERVER
        print("  [BROWSER] 正在启动浏览器...", flush=True)
        page = launch(**launch_args)
        page.get(TARGET_URL)
        page.wait.doc_loaded(timeout=20)
        page.wait(5)

        # ---------- 登录（HAX 风格 Cookie 优先） ----------
        login_success = False
        if session_token:
            print("  [LOGIN] 尝试使用 session_token 快速登录...", flush=True)
            # set_session_cookie 内部已经访问 /login 预热，无需外部再 page.get(TARGET_URL)
            cookie_ok = set_session_cookie(page, session_token, proxies=proxies)
            if cookie_ok:
                # 连续两次访问 /vps-info 让服务端与前端路由都生效
                try:
                    page.get("https://woiden.id/vps-info")
                    page.wait.doc_loaded(timeout=15)
                    page.wait(2)
                    page.get("https://woiden.id/vps-info")
                    page.wait.doc_loaded(timeout=15)
                    page.wait(2)
                except Exception as e:
                    debug_print(f"导航 vps-info 失败: {e}")

                if is_logged_in(page):
                    print("  ✅ Cookie 登录成功", flush=True)
                    login_success = True
                else:
                    print("  ⚠️ Cookie 未生效，将执行 OAuth", flush=True)
                    try:
                        snippet = page.run_js(
                            "document.body.innerText.substring(0, 300)"
                        ) or ""
                        debug_print(f"  [DEBUG] 页面片段: {snippet[:200]}")
                    except Exception:
                        pass
            else:
                print("  ⚠️ Cookie 注入失败，将执行 OAuth", flush=True)
        else:
            print("  [LOGIN] 未提供 session_token，直接走 OAuth", flush=True)

        if not login_success:
            # ---------- Consent 弹窗 ----------
            for selector in [
                "text:Consent", "text:同意", "text:I agree",
                "text:Accept", "text:Accept all", "text:Agree",
                "css:button[aria-label='Consent']",
                "css:#didomi-notice-agree-button", "css:.didomi-button",
                "css:button[class*='consent']", "css:button[class*='agree']",
            ]:
                try:
                    consent_btn = page.ele(selector, timeout=2)
                    if consent_btn and consent_btn.is_displayed:
                        consent_btn.click_self(by_js=True)
                        print("已点击 Consent 同意按钮")
                        page.wait(2)
                        break
                except Exception:
                    pass

            # ---------- Telegram OAuth 兜底 ----------
            iframe_xpath = "xpath://iframe[contains(@src, 'oauth.telegram.org')]"
            frame_found = False
            for _ in range(10):
                try:
                    if page.ele(iframe_xpath, timeout=2):
                        frame_found = True
                        break
                except Exception:
                    pass
                page.wait(1)
            if not frame_found:
                raise RuntimeError("未找到 Telegram OAuth iframe")
            with page.with_frame(iframe_xpath) as frame_page:
                tg_login_btn = frame_page.ele("css:button.tgme_widget_login_button", timeout=5)
                if not tg_login_btn:
                    raise RuntimeError("未找到 Telegram 登录按钮")
                tg_login_btn.click_self(by_js=True)
            page.wait(3)
            if "woiden.id/vps-info" not in page.url:
                oauth_tab_id = None
                for _ in range(10):
                    for tab_id in page.tab_ids:
                        oauth_page = page.get_tab(tab_id)
                        if "oauth.telegram.org" in (oauth_page.url or ""):
                            oauth_tab_id = tab_id
                            break
                    if oauth_tab_id:
                        break
                    page.wait(1)
                if not oauth_tab_id:
                    raise RuntimeError("未找到 oauth.telegram.org tab")
                oauth_page = page.get_tab(oauth_tab_id)
                oauth_page.activate()
                oauth_page.wait.doc_loaded(timeout=20)
                phone_input = oauth_page.ele("css:#login-phone-code")
                if not phone_input:
                    raise RuntimeError("未找到手机号输入框")
                phone_input.input(phone, clear=True)
                oauth_page.wait(2)
                continue_btn = (oauth_page.ele("text:继续")
                                or oauth_page.ele("css:button[type='submit']")
                                or oauth_page.ele("css:button"))
                if continue_btn:
                    continue_btn.click_self()
                else:
                    oauth_page.run_js("document.querySelector('form')?.submit();")
                try:
                    page.to_tab(page.tab_id)
                except Exception:
                    pass
                for _ in range(60):
                    page.wait(2)
                    if "woiden.id/vps-info" in (page.url or ""):
                        break
                else:
                    raise RuntimeError("登录超时，未跳转到 vps-info")

        if not is_logged_in(page):
            page.get("https://woiden.id/vps-info")
            page.wait.doc_loaded(timeout=15)
            if not is_logged_in(page):
                raise RuntimeError("无法确认登录状态")
        print("  ✅ 登录成功", flush=True)

        # ---------- 检测是否需要续期 ----------
        page.get("https://woiden.id/vps-info")
        page.wait.doc_loaded(timeout=15)
        page.wait(2)
        try:
            should_renew, valid_until, remaining_hours = check_should_renew(page)
        except Exception as e:
            print(f"  [CHECK] 检查异常: {e}，继续续期")
            should_renew, valid_until, remaining_hours = True, None, None

        if not should_renew:
            print(f"  ⏭️ 已续期（剩余 {remaining_hours:.1f} 小时），跳过", flush=True)
            notify_renewal_skipped(phone, valid_until, remaining_hours, bot_token, chat_id)
            return "skipped", {"valid_until": valid_until, "remaining_hours": remaining_hours}

        # ---------- 进入续期页面 ----------
        renew_link = None
        for sel in ['css:a[href="/vps-renew/"]', 'text:Renew VPS', 'text:续订VPS']:
            try:
                renew_link = page.ele(sel)
                if renew_link and renew_link.is_displayed:
                    break
            except Exception:
                pass
        if not renew_link:
            raise RuntimeError("未找到 续订VPS 按钮")
        renew_link.click_self(by_js=True)
        page.wait(3)

        handle_ad_wall(page)
        if "woiden.id/vps-renew" not in page.url:
            page.get("https://woiden.id/vps-renew/")
            page.wait.doc_loaded(timeout=15)
            page.wait(3)

        # 域名输入
        print("  [FORM] 输入域名...")
        web_input = page.ele('css:#web_address')
        if web_input:
            try:
                page.actions.move_to(web_input, duration=1).click().perform()
                page.wait(0.3)
                web_input.clear()
                for ch in "woiden.id":
                    web_input.input(ch)
                    time.sleep(0.05)
                page.run_js("document.querySelector('#web_address').blur();")
            except Exception as e:
                print(f"    鼠标输入失败: {e}，使用 JS 强制写入")
                _hard_set_value(page, "woiden.id", '#web_address', 'input[name="web_address"]')

            readback = page.run_js("document.querySelector('#web_address').value") or ""
            if readback.strip() != "woiden.id":
                print(f"    ❌ 域名输入验证失败，当前值: '{readback}'，尝试重写...")
                _hard_set_value(page, "woiden.id", '#web_address', 'input[name="web_address"]')
                page.run_js("document.querySelector('#web_address').blur();")
                readback2 = page.run_js("document.querySelector('#web_address').value") or ""
                if readback2.strip() == "woiden.id":
                    print("    ✅ 重写后验证通过")
                else:
                    print("    ⚠️ 重写后值仍不正确，继续尝试")
            else:
                print("    ✅ 域名输入成功")
        else:
            print("  ⚠️ 未找到 #web_address")

        agreement = page.ele('css:input[name="agreement"][value="yes"]')
        if agreement and not agreement.is_checked:
            agreement.click_self(by_js=True)
            print("  [FORM] 勾选协议")

        # 算式验证码
        captcha_filled = False
        for attempt in range(3):
            result = solve_math_captcha(page)
            if result:
                captcha_input = page.ele('css:#captcha')
                if captcha_input:
                    try:
                        captcha_input.input(result, clear=True)
                    except Exception:
                        pass
                    readback = page.run_js("document.querySelector('#captcha').value") or ""
                    if readback.strip() == result:
                        print(f"  [CAPTCHA] 已输入: {result}")
                        captcha_filled = True
                        break
                    else:
                        ok, _, _ = _hard_set_value(page, result, '#captcha', 'input[name="captcha"]')
                        if ok:
                            print(f"  [CAPTCHA] 已输入(JS): {result}")
                            captcha_filled = True
                            break
            page.wait(1)
        if not captcha_filled:
            raise RuntimeError("算式验证码输入失败")

        print("  [CF] 等待 CloudFlare 验证 (10s)...")
        page.wait(10)

        final_domain = page.run_js("document.querySelector('#web_address').value") or ""
        if final_domain.strip() != "woiden.id":
            print(f"  ⚠️ 提交前域名仍不正确 ('{final_domain}')，强制修正")
            _hard_set_value(page, "woiden.id", '#web_address', 'input[name="web_address"]')

        renew_btn = page.ele("css:button[name=submit_button][type=button].btn-primary")
        if not renew_btn:
            raise RuntimeError("未找到 Renew VPS 按钮")
        renew_btn.click_self(by_js=True)
        print("  [FORM] 已点击 Renew VPS")
        page.wait(5)
        close_ads(page)

        # 响应检测
        print("  [RESPONSE] 等待提交响应...")
        found = False
        resp_text = ""
        for i in range(25):
            page.wait(2)
            try:
                resp_text = page.run_js("(function(){var r=document.querySelector('#response');return r?r.textContent.trim():'';})()") or ""
            except Exception:
                resp_text = ""
            if resp_text:
                print(f"  [RESPONSE] 第{i+1}次检测: #response = '{resp_text[:80]}'")
                if "verification code has been sent" in resp_text.lower():
                    print("  [RESPONSE] ✅ 检测到 'verification code has been sent'")
                    found = True
                    break
                if "correct site address" in resp_text.lower():
                    print("  [RESPONSE] ❌ 服务器反馈域名错误，重新设置域名并重试...")
                    _hard_set_value(page, "woiden.id", '#web_address', 'input[name="web_address"]')
                    renew_btn.click_self(by_js=True)
                    print("  [FORM] 重新点击 Renew VPS")
                    page.wait(5)
                    continue
            try:
                body_text = page.run_js("document.body.innerText") or ""
            except Exception:
                body_text = ""
            if body_text and "verification code has been sent" in body_text.lower():
                print(f"  [RESPONSE] 第{i+1}次检测: body 包含关键字")
                resp_text = body_text
                found = True
                break
            if i % 5 == 0:
                print(f"  [RESPONSE] 当前URL: {page.url}")

        if not found:
            page.wait(10)
            try:
                body_text = page.run_js("document.body.innerText") or ""
            except Exception:
                body_text = ""
            if "verification code has been sent" in body_text.lower():
                resp_text = body_text
                found = True
            else:
                print(f"  [RESPONSE] 未检测到关键字，页面内容片段:\n{body_text[:500]}")
                try:
                    take_screenshot(page, f"no_response_{phone}.png", bot_token, chat_id,
                                    f"未检测到响应 - {phone}")
                except Exception:
                    pass

        if not found:
            raise RuntimeError("提交未成功，未收到验证码发送提示")
        print("  [RESPONSE] ✅ 提交成功，验证码已发送到 Telegram")

        # 跳转到续期码输入页
        code_link = page.ele('css:a.btn[href="/vps-renew-code"]') or page.ele('text:INPUT RENEW CODE')
        if code_link:
            code_link.click_self(by_js=True)
        else:
            page.get("https://woiden.id/vps-renew-code")
        page.wait.doc_loaded(timeout=15)
        page.wait(3)

        # ---------- 获取续期码 ----------
        print("  [CODE] 获取续期码...")
        if os.path.exists(code_file):
            open(code_file, 'w').close()

        TG_RENEW_CODE = read_code_from_file(code_file, consume=False)
        if TG_RENEW_CODE:
            print(f"  [CODE] 从文件读取到续期码: {TG_RENEW_CODE[:20]}***")
        else:
            print("  [CODE] 文件无内容，尝试从聊天历史获取...")
            if account_index <= len(SESSION_STRINGS):
                ss = SESSION_STRINGS[account_index - 1]
                if ss:
                    print(f"  [CODE] 使用当前账号对应的 SESSION_STRING_{account_index} (长度 {len(ss)})")
                    code = get_code_from_history(ss)
                    if code:
                        write_code_to_file(code_file, code)
                        TG_RENEW_CODE = code
                        print(f"  [CODE] 从聊天历史获取到续期码: {TG_RENEW_CODE[:20]}***")
                else:
                    print(f"  [CODE] 账号 {account_index} 未配置 SESSION_STRING")
            else:
                print(f"  [CODE] 账号 {account_index} 超过 SESSION_STRINGS 数量")

            if not TG_RENEW_CODE:
                print("  [CODE] 历史记录未找到，回退到轮询 Telegram Bot API...")
                all_bots = []
                seen = set()
                for acc in ACCOUNTS:
                    t = acc.get("bot_token")
                    if t and t not in seen:
                        seen.add(t)
                        all_bots.append({"token": t, "label": f"...{t[-6:]}"})
                if bot_token and bot_token not in seen:
                    all_bots.insert(0, {"token": bot_token, "label": f"...{bot_token[-6:]}"})

                code, src = get_renewal_code_from_telegram(
                    all_bots, code_file,
                    timeout=600, poll_interval=5
                )
                if not code:
                    raise RuntimeError("未获取到续期码")
                TG_RENEW_CODE = code
                print(f"  [CODE] 从 Telegram 轮询获取到续期码: {TG_RENEW_CODE[:20]}***")

        # ---------- 填入续期码和算术验证码 ----------
        captcha2 = solve_math_captcha(page)
        captcha2_value = ""
        if captcha2:
            captcha2_value = str(captcha2)
            captcha_input = page.ele('css:#captcha')
            if captcha_input:
                try:
                    captcha_input.input(captcha2_value, clear=True)
                except Exception:
                    pass
                page.wait(0.5)
                rb = page.run_js("document.querySelector('#captcha').value") or ""
                if rb.strip() != captcha2_value:
                    ok, _, _ = _hard_set_value(page, captcha2_value, '#captcha', 'input[name="captcha"]')
                    if ok:
                        print(f"  [CAPTCHA2] 已输入(JS): {captcha2_value}")
                    else:
                        print(f"  [CAPTCHA2] ⚠️ 输入失败")
                else:
                    print(f"  [CAPTCHA2] 已输入: {captcha2_value}")
            else:
                print(f"  [CAPTCHA2] ⚠️ 未找到 #captcha 输入框")

        vcode_input = page.ele("css:input.form-control:not(#captcha)") or page.ele("css:input[name=code]")
        if vcode_input:
            try:
                vcode_input.input(TG_RENEW_CODE, clear=True)
            except Exception:
                pass
            page.wait(0.5)
            code_rb = page.run_js("(function(){var e=document.querySelector('input[name=code]')||document.querySelector('#code');return e?e.value:'';})()") or ""
            if code_rb.strip() != TG_RENEW_CODE.strip():
                ok, _, _ = _hard_set_value(page, TG_RENEW_CODE, 'input[name="code"]', '#code')
                if ok:
                    print(f"  [CODE] 续期码已输入(JS): {TG_RENEW_CODE[:20]}***")
                else:
                    print(f"  [CODE] ⚠️ 续期码输入失败，页面值: '{code_rb[:20]}'")
            else:
                print(f"  [CODE] 续期码已输入: {TG_RENEW_CODE[:20]}***")
        else:
            print(f"  [CODE] ⚠️ 未找到续期码输入框")

        # ---------- reCAPTCHA ----------
        print("  [reCAPTCHA] 处理音频验证...")
        recaptcha_ok = solve_recaptcha(page, timeout=90)
        if not recaptcha_ok:
            print("  [reCAPTCHA] 自动失败，等待手动 60s...")
            page.wait(60)
            recaptcha_ok = is_recaptcha_solved(page)

        # ============================================================
        # ---------- 提交续期（用 requests 直接 POST） ----------
        # ============================================================
        print("  [SUBMIT] 提交前确认 reCAPTCHA 状态...", flush=True)
        if not is_recaptcha_solved(page):
            print("  [SUBMIT] ⚠️ reCAPTCHA 未通过，再等 30s...", flush=True)
            page.wait(30)
            if not is_recaptcha_solved(page):
                raise RuntimeError("reCAPTCHA 未通过，无法提交")

        try:
            code_value = page.run_js("(function(){var e=document.querySelector('input[name=code]')||document.querySelector('#code');return e?e.value:'';})()") or ""
        except Exception:
            code_value = ""
        if not code_value.strip():
            code_value = TG_RENEW_CODE
            print(f"  [SUBMIT] 页面 code 为空，用 Python 变量: {code_value[:20]}***", flush=True)
        else:
            print(f"  [SUBMIT] 从页面取 code: {code_value[:20]}***", flush=True)

        try:
            captcha_value = page.run_js("(function(){var e=document.querySelector('input[name=captcha]')||document.querySelector('#captcha');return e?e.value:'';})()") or ""
        except Exception:
            captcha_value = ""
        if not captcha_value.strip():
            captcha_value = captcha2_value
            print(f"  [SUBMIT] 页面 captcha 为空，用 Python 变量: {captcha_value}", flush=True)
        else:
            print(f"  [SUBMIT] 从页面取 captcha: {captcha_value}", flush=True)

        recaptcha_value = get_recaptcha_token(page)
        if not recaptcha_value:
            raise RuntimeError("无法获取 reCAPTCHA token")
        print(f"  [SUBMIT] reCAPTCHA token 长度: {len(recaptcha_value)}", flush=True)

        cookies_dict = {}
        try:
            for c in page.get_cookies():
                name = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else None)
                value = getattr(c, "value", None) or (c.get("value") if isinstance(c, dict) else None)
                if name:
                    cookies_dict[name] = value
        except Exception as e:
            print(f"  [SUBMIT] 获取 cookies 失败: {e}", flush=True)

        if not cookies_dict:
            raise RuntimeError("无法获取浏览器 cookies")
        print(f"  [SUBMIT] cookies 数量: {len(cookies_dict)}", flush=True)

        submit_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://woiden.id",
            "Referer": "https://woiden.id/vps-renew-code",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        submit_data = {
            "code": code_value,
            "captcha": captcha_value,
            "g-recaptcha-response": recaptcha_value,
        }

        print("  [SUBMIT] 使用 requests POST /renew-vps-verification/ ...", flush=True)
        try:
            submit_resp = req_lib.post(
                "https://woiden.id/renew-vps-verification/",
                data=submit_data,
                headers=submit_headers,
                cookies=cookies_dict,
                proxies=proxies,
                timeout=30,
                allow_redirects=False,
            )
        except Exception as e:
            raise RuntimeError(f"提交请求异常: {e}")

        print(f"  [SUBMIT] HTTP {submit_resp.status_code}, 响应长度 {len(submit_resp.text)}", flush=True)
        result_html = submit_resp.text or ""

        try:
            page.run_js(
                "(function(html){var r=document.getElementById('response');if(r)r.innerHTML=html;})("
                + json.dumps(result_html) + ")"
            )
        except Exception:
            pass

        # ---------- 结果判断 ----------
        print("  [RESULT] 检查提交响应...", flush=True)

        resp_div_text = ""
        m = re.search(r'<div[^>]*id=["\']response["\'][^>]*>(.*?)</div>',
                      result_html, re.DOTALL | re.IGNORECASE)
        if m:
            resp_div_text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        if not resp_div_text:
            resp_div_text = re.sub(r'<[^>]+>', ' ', result_html)
            resp_div_text = re.sub(r'\s+', ' ', resp_div_text).strip()

        result_text = resp_div_text
        result_lower = result_text.lower()
        print(f"  [RESULT] 响应文本片段: {result_text[:200]}", flush=True)

        success_keywords = [
            "your vps has been renewed",
            "renewed successfully",
            "renewal successful",
            "subscription renewed",
            "subscription successfully",
            "renewed",
            "续期成功",
        ]
        fail_keywords = [
            "incorrect",
            "invalid code",
            "wrong code",
            "invalid captcha",
            "captcha failed",
            "captcha 验证失败",
            "验证码错误",
            "renew failed",
            "failed to renew",
            "verification code is invalid",
        ]

        is_success = any(kw in result_lower for kw in success_keywords)

        expiry_date = None
        if is_success:
            print("  [RESULT] ✅ 检测到续期成功！", flush=True)
            for pat in [
                r"until\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4})",
                r"[Ee]xpir(?:e|y)[:\s]*(\d{4}-\d{2}-\d{2})",
                r"[Vv]alid.*[Uu]ntil[:\s]*(\d{4}-\d{2}-\d{2})",
                r"到期[：:]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            ]:
                mm = re.search(pat, result_text)
                if mm:
                    expiry_date = mm.group(1)
                    print(f"  [RESULT] 到期日: {expiry_date}", flush=True)
                    break
            notify_renewal_success(phone, expiry_date or "未知日期", bot_token, chat_id)
            return "success", {"expiry_date": expiry_date}

        if any(kw in result_lower for kw in fail_keywords):
            error_msg = "#response 内容表示失败"
        elif not result_text:
            error_msg = "响应体为空"
        else:
            error_msg = "#response 内容无法识别"
        print(f"  [RESULT] ❌ 续期失败: {error_msg}", flush=True)
        print(f"  [RESULT] 完整响应: {result_text[:500]}", flush=True)
        notify_renewal_failed(phone, "结果页", error_msg, bot_token, chat_id)
        return "failed", {"step": "结果页", "error": error_msg}

    except Exception as e:
        print(f"  ❌ 异常: {e}", flush=True)
        traceback.print_exc()
        if page:
            try:
                take_screenshot(page, f"error_{phone}.png", bot_token, chat_id, f"异常 - {phone}")
            except Exception:
                pass
        notify_renewal_failed(phone, "执行异常", str(e), bot_token, chat_id)
        return "failed", {"step": "执行异常", "error": str(e)}
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass


# ========== 主入口 ==========
if __name__ == "__main__":
    print("#########################")
    print("   Woiden 自动续期（HAX 风格 Cookie 登录版）")
    print("#########################")
    if not ACCOUNTS:
        print("❌ 未加载账号，请设置 ACCOUNTS_JSON", flush=True)
        sys.exit(1)
    print(f"✅ 加载了 {len(ACCOUNTS)} 个账号", flush=True)
    print(f"✅ 跳过阈值: {SKIP_THRESHOLD_HOURS} 小时", flush=True)
    print(f"✅ 最大轮数: {MAX_RENEW_ROUNDS}", flush=True)
    print(f"✅ 进度通知: {'开启' if NOTIFY_PROGRESS else '关闭'}", flush=True)

    total = len(ACCOUNTS)

    summary_bot = ""
    summary_chat = ""
    for acc in ACCOUNTS:
        if acc.get("bot_token") and acc.get("chat_id"):
            summary_bot = acc["bot_token"]
            summary_chat = acc["chat_id"]
            break
    if not summary_bot:
        for acc in ACCOUNTS:
            if acc.get("bot_token"):
                summary_bot = acc["bot_token"]
                summary_chat = acc.get("chat_id", "")
                break

    success_set = set()
    skipped_set = set()

    pending = [(i, acc) for i, acc in enumerate(ACCOUNTS, 1)]
    round_no = 0

    while pending and round_no < MAX_RENEW_ROUNDS:
        round_no += 1
        print(f"\n{'#'*60}")
        print(f"  第 {round_no}/{MAX_RENEW_ROUNDS} 轮，待处理 {len(pending)} 个账号")
        print(f"{'#'*60}", flush=True)

        if round_no > 1 and NOTIFY_PROGRESS:
            try:
                notify_round_start(round_no, MAX_RENEW_ROUNDS,
                                   [a for _, a in pending],
                                   summary_bot, summary_chat)
            except Exception as e:
                print(f"  [NOTIFY] 轮次开始通知失败: {e}", flush=True)

        failed_in_round = []
        accounts_this_round = pending

        for i_in_round, (orig_idx, acc) in enumerate(accounts_this_round, 1):
            phone = acc.get("phone", f"account_{orig_idx}")
            print(f"\n===== [第{round_no}轮] 处理 {i_in_round}/{len(accounts_this_round)}: {phone} (原索引 {orig_idx}) =====", flush=True)

            status = "failed"
            info = {"step": "未知", "error": "未知"}
            try:
                status, info = renew_account(acc, account_index=orig_idx)
            except Exception as e:
                print(f"  ⚠️ 账号处理异常: {e}", flush=True)
                traceback.print_exc()
                status = "failed"
                info = {"step": "主循环异常", "error": str(e)}

            if status == "success":
                success_set.add(orig_idx)
                emoji = "✅"
                status_text = "续期成功"
            elif status == "skipped":
                skipped_set.add(orig_idx)
                emoji = "⏭️"
                rh = info.get("remaining_hours")
                status_text = f"已续期跳过（剩余 {rh:.1f}h）" if isinstance(rh, (int, float)) else "已续期跳过"
            else:
                failed_in_round.append((orig_idx, acc))
                emoji = "❌"
                status_text = f"失败（{info.get('step', '')}: {info.get('error', '')}）"

            remaining_in_round = accounts_this_round[i_in_round:]
            pending_phones = (
                [a.get("phone", "?") for _, a in remaining_in_round] +
                [a.get("phone", "?") for _, a in failed_in_round]
            )

            if NOTIFY_PROGRESS:
                try:
                    notify_progress(
                        current_idx=i_in_round,
                        total=len(accounts_this_round),
                        phone=phone,
                        status_emoji=emoji,
                        status_text=status_text,
                        pending_list=pending_phones,
                        bot_token=acc.get("bot_token", "") or summary_bot,
                        chat_id=acc.get("chat_id", "") or summary_chat,
                    )
                except Exception as e:
                    print(f"  [NOTIFY] 进度通知失败: {e}", flush=True)

            if i_in_round < len(accounts_this_round):
                time.sleep(random.randint(10, 30))

        pending = failed_in_round

        if pending:
            print(f"\n[ROUND {round_no}] 本轮结束，仍有 {len(pending)} 个账号未完成", flush=True)
            will_retry = round_no < MAX_RENEW_ROUNDS
            if NOTIFY_PROGRESS:
                try:
                    notify_round_end(round_no, will_retry,
                                     [a for _, a in pending],
                                     summary_bot, summary_chat)
                except Exception as e:
                    print(f"  [NOTIFY] 轮次结束通知失败: {e}", flush=True)

            if will_retry:
                delay = random.randint(60, 120)
                print(f"  轮次间隔等待 {delay} 秒...", flush=True)
                time.sleep(delay)
        else:
            break

    final_failed = [a.get("phone", "?") for _, a in pending]
    final_failed_detail = [(idx, a.get("phone", "?")) for idx, a in pending]

    try:
        notify_all_done(
            total=total,
            success=len(success_set),
            failed=len(final_failed),
            skipped=len(skipped_set),
            failed_list=final_failed,
            bot_token=summary_bot,
            chat_id=summary_chat,
        )
    except Exception as e:
        print(f"  [NOTIFY] 总结通知失败: {e}", flush=True)

    print(f"\n最终结果: 成功 {len(success_set)} / 跳过 {len(skipped_set)} / 失败 {len(final_failed)} / 共 {total} 个账号", flush=True)
    print(f"总轮数: {round_no}", flush=True)
    if final_failed_detail:
        print("仍失败的账号：", flush=True)
        for idx, phone in final_failed_detail:
            print(f"  - 索引 {idx}: {phone}", flush=True)
