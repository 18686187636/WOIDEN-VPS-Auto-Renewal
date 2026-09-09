#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Woiden VPS 自动续期（GitHub Actions 版）
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
from datetime import datetime, timezone, timedelta

import requests as req_lib
from ruyipage import launch, Keys

# ========== 环境变量 ==========
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON", "[]")
ACCOUNTS = json.loads(ACCOUNTS_JSON)
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
PROXY_SERVER = os.getenv("PROXY_SERVER", "")   # 例如 http://127.0.0.1:1081
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# ========== 全局常量 ==========
TARGET_URL = "https://woiden.id/login"
RENEW_CODE_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')

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
    except:
        return False

def get_proxies():
    if not PROXY_SERVER:
        return None
    # 若代理是 http/https，直接返回
    if PROXY_SERVER.startswith(('http://', 'https://')):
        return {"http": PROXY_SERVER, "https": PROXY_SERVER}
    # 若为 socks5，检测端口是否开放（简化）
    try:
        if is_port_open('127.0.0.1', 1080) or is_port_open('127.0.0.1', 1081):
            return {"http": PROXY_SERVER, "https": PROXY_SERVER}
    except:
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
            resp = req_lib.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
            return resp.json().get("ok", False)
        except Exception:
            return False

def notify_renewal_success(phone, expiry_date, bot_token, chat_id):
    msg = f"✅ <b>VPS 续期成功</b>\n\nWoiden\n📱 {phone}\n📅 {expiry_date or '未知'}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

def notify_renewal_failed(phone, step, error, bot_token, chat_id):
    msg = f"❌ <b>VPS 续期失败</b>\n\nWoiden\n📱 {phone}\n📍 {step}\n⚠️ {error}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

def take_screenshot(page, path, bot_token, chat_id, caption):
    try:
        driver = None
        if hasattr(page, 'driver'):
            driver = page.driver
        elif hasattr(page, '_driver'):
            driver = page._driver
        elif hasattr(page, 'page'):
            driver = page.page
        if driver and hasattr(driver, 'get_screenshot_as_file'):
            driver.get_screenshot_as_file(path)
        else:
            if hasattr(page, 'screenshot'):
                page.screenshot(path)
            elif hasattr(page, 'get_screenshot'):
                page.get_screenshot(path)
            else:
                raise Exception("无可用截图方法")
        if os.path.exists(path):
            # 可发送到 Telegram（但 GitHub Actions 中通过 artifact 上传）
            pass
    except Exception as e:
        print(f"  [截图] 失败: {e}", flush=True)

# ========== 续期码文件读写（支持指定路径） ==========
def read_code_from_file(code_file):
    try:
        if os.path.exists(code_file):
            with open(code_file, 'r', encoding='utf-8') as f:
                code = f.read().strip()
            if code and RENEW_CODE_PATTERN.search(code):
                print(f"  [文件] ✅ 从 {code_file} 读取到续期码: {code[:20]}...")
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

# ========== Telegram 轮询获取续期码（多 Bot，并持续检查文件） ==========
def get_renewal_code_from_telegram(bot_tokens, page, phone, bot_token, chat_id,
                                   code_file, timeout=1800, poll_interval=10):
    offsets = {}
    for bt in bot_tokens:
        try:
            proxies = get_proxies()
            url = f"https://api.telegram.org/bot{bt['token']}/getUpdates"
            resp = req_lib.get(url, timeout=10, proxies=proxies) if proxies else req_lib.get(url, timeout=10)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                offsets[bt['token']] = max(u["update_id"] for u in data["result"]) + 1
            else:
                offsets[bt['token']] = 0
        except Exception:
            offsets[bt['token']] = 0
    elapsed = 0
    code = ""
    while elapsed < timeout:
        # 每轮先检查文件
        file_code = read_code_from_file(code_file)
        if file_code:
            print(f"  [CODE] 从文件 {code_file} 读取到续期码，直接使用", flush=True)
            return file_code, "file"
        # 轮询 Telegram
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
            except Exception:
                pass
        time.sleep(poll_interval)
        elapsed += poll_interval
        if elapsed % 60 < poll_interval:
            print(f"  [CODE] 等待中... ({elapsed//60} 分钟)", flush=True)
    return "", None

# ========== 页面操作函数（登录检测、Cookie、验证码等） ==========
def is_logged_in(page):
    try:
        logout_btn = page.ele("xpath://*[contains(text(), 'Logout') or contains(text(), 'Log out')]", timeout=2)
        if logout_btn and logout_btn.is_displayed:
            return True
        login_btn = page.ele("xpath://*[contains(text(), 'Login')]", timeout=2)
        if login_btn and login_btn.is_displayed:
            return False
        if "woiden.id/vps-info" in page.url:
            menu = page.ele("css:a.nav-link.dropdown-toggle", timeout=2)
            if menu and menu.is_displayed:
                return True
        return False
    except:
        return False

def set_session_cookie(page, session_token):
    try:
        page.set_cookies([{"name": "PHPSESSID", "value": session_token, "domain": ".woiden.id", "path": "/"}])
        return True
    except Exception:
        pass
    try:
        page.run_js(f"document.cookie = 'PHPSESSID={session_token}; path=/; domain=.woiden.id; SameSite=Lax';")
        return True
    except Exception:
        pass
    return False

def solve_math_captcha(page):
    """识别页面上的算式验证码（从 URL 或像素匹配），返回结果字符串"""
    from PIL import Image, ImageDraw, ImageFont
    import urllib.request
    import os

    # ...（完整函数体与 ceshi.py 相同，此处省略以节省篇幅，实际需完整复制）
    # 请从原 ceshi.py 中完整复制 solve_math_captcha 函数

def close_ads(page):
    """关闭广告弹窗（与 ceshi.py 相同）"""
    # 完整复制原函数

def handle_ad_wall(page):
    """处理 FreeContainers 广告墙（与 ceshi.py 相同）"""
    # 完整复制原函数

def _hard_set_value(page, value, *selectors):
    """强制写入输入框（与 ceshi.py 相同）"""
    # 完整复制原函数

def solve_recaptcha(page, timeout=60):
    """reCAPTCHA 音频求解（与 ceshi.py 相同）"""
    # 完整复制原函数

# ========== 单账号续期主流程 ==========
def renew_account(account):
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

        # ---------- 尝试 Cookie 登录 ----------
        login_success = False
        if session_token:
            print("  [LOGIN] 尝试使用 session_token 快速登录...", flush=True)
            page.get(TARGET_URL)
            set_session_cookie(page, session_token)
            page.get("https://woiden.id/vps-info")
            page.wait.doc_loaded(timeout=15)
            page.get("https://woiden.id/vps-info")
            page.wait.doc_loaded(timeout=10)
            if is_logged_in(page):
                print("  ✅ Cookie 登录成功", flush=True)
                login_success = True
            else:
                print("  ⚠️ Cookie 未生效，将执行 OAuth", flush=True)

        if not login_success:
            # 处理 Consent
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

            # Telegram OAuth 登录（与 ceshi.py 相同）
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
                continue_btn = oauth_page.ele("text:继续") or oauth_page.ele("css:button[type='submit']") or oauth_page.ele("css:button")
                if continue_btn:
                    continue_btn.click_self()
                else:
                    oauth_page.run_js("document.querySelector('form')?.submit();")
                try:
                    page.to_tab(page.tab_id)
                except:
                    pass
                for _ in range(60):
                    page.wait(2)
                    if "woiden.id/vps-info" in (page.url or ""):
                        break
                else:
                    raise RuntimeError("登录超时，未跳转到 vps-info")

        # 确认登录
        if not is_logged_in(page):
            page.get("https://woiden.id/vps-info")
            page.wait.doc_loaded(timeout=15)
            if not is_logged_in(page):
                raise RuntimeError("无法确认登录状态")
        print("  ✅ 登录成功", flush=True)

        # ---------- 进入续期页面 ----------
        renew_link = None
        for sel in ['css:a[href="/vps-renew/"]', 'text:Renew VPS', 'text:续订VPS']:
            try:
                renew_link = page.ele(sel)
                if renew_link and renew_link.is_displayed:
                    break
            except:
                pass
        if not renew_link:
            raise RuntimeError("未找到 续订VPS 按钮")
        renew_link.click_self(by_js=True)
        page.wait(3)

        # 处理广告墙
        handle_ad_wall(page)
        if "woiden.id/vps-renew" not in page.url:
            page.get("https://woiden.id/vps-renew/")
            page.wait.doc_loaded(timeout=15)
            page.wait(3)

        # 填写域名和协议
        web_input = page.ele('css:#web_address')
        if web_input:
            web_input.input("woiden.id", clear=True)
            print("  [FORM] 输入域名: woiden.id")
        agreement = page.ele('css:input[name="agreement"][value="yes"]')
        if agreement and not agreement.is_checked:
            agreement.click_self(by_js=True)
            print("  [FORM] 勾选协议")

        # 填写算式验证码（在点击提交之前）
        captcha_filled = False
        for _ in range(3):
            result = solve_math_captcha(page)
            if result:
                captcha_input = page.ele('css:#captcha')
                if captcha_input:
                    captcha_input.input(str(result), clear=True)
                    print(f"  [CAPTCHA] 输入: {result}")
                    captcha_filled = True
                    break
            page.wait(1)
        if not captcha_filled:
            raise RuntimeError("算式验证码输入失败")

        # 等待 CloudFlare
        print("  [CF] 等待 CloudFlare 验证 (10s)...")
        page.wait(10)

        # 点击 Renew VPS
        renew_btn = page.ele("css:button[name=submit_button][type=button].btn-primary")
        if not renew_btn:
            raise RuntimeError("未找到 Renew VPS 按钮")
        renew_btn.click_self(by_js=True)
        print("  [FORM] 已点击 Renew VPS")
        page.wait(5)
        close_ads(page)

        # 检查响应
        resp_text = ""
        for _ in range(15):
            page.wait(2)
            resp_text = page.run_js("(function(){var r=document.querySelector('#response');return r?r.textContent.trim():'';})()") or ""
            if resp_text:
                break
            body = page.run_js("document.body.innerText") or ""
            if "verification code has been sent" in body.lower():
                resp_text = body
                break
        if not resp_text or "verification code has been sent" not in resp_text.lower():
            raise RuntimeError("提交未成功，未收到验证码发送提示")

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
        # 清空当前文件
        if os.path.exists(code_file):
            open(code_file, 'w').close()
        TG_RENEW_CODE = read_code_from_file(code_file)
        if TG_RENEW_CODE:
            print(f"  [CODE] 从文件读取到续期码: {TG_RENEW_CODE[:20]}***")
        else:
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
                all_bots, page, phone, bot_token, chat_id, code_file,
                timeout=1800, poll_interval=10
            )
            if not code:
                raise RuntimeError("未获取到续期码")
            TG_RENEW_CODE = code
            print(f"  [CODE] 从 Telegram 获取到续期码: {TG_RENEW_CODE[:20]}***")

        # 填写算式验证码（输入页）
        captcha2 = solve_math_captcha(page)
        if captcha2:
            captcha_input = page.ele('css:#captcha')
            if captcha_input:
                captcha_input.input(str(captcha2), clear=True)

        # 填入续期码
        vcode_input = page.ele("css:input.form-control:not(#captcha)") or page.ele("css:input[name=code]")
        if vcode_input:
            vcode_input.input(TG_RENEW_CODE, clear=True)

        # reCAPTCHA
        print("  [reCAPTCHA] 处理音频验证...")
        recaptcha_ok = solve_recaptcha(page, timeout=90)
        if not recaptcha_ok:
            print("  [reCAPTCHA] 自动失败，等待手动 60s...")
            page.wait(60)
            recaptcha_ok = is_recaptcha_solved(page)  # 需实现

        # 提交
        submit_btn = page.ele("css:button[name=submit_button]") or page.ele("css:button.btn-primary")
        if not submit_btn:
            raise RuntimeError("未找到提交按钮")
        submit_btn.click_self(by_js=True)
        time.sleep(60)

        # 检查结果
        close_ads(page)
        result_text = page.run_js("document.body.innerText") or ""
        result_lower = result_text.lower()
        success = any(kw in result_lower for kw in ["renewed successfully", "renewal successful", "续期成功"])
        expiry = None
        if success:
            for pat in [r"until\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4})", r"[Ee]xpir(?:e|y)[:\s]*(\d{4}-\d{2}-\d{2})"]:
                m = re.search(pat, result_text)
                if m:
                    expiry = m.group(1)
                    break
            notify_renewal_success(phone, expiry or "未知", bot_token, chat_id)
            return True
        else:
            error_msg = "Captcha 失败" if "captcha" in result_lower else "未知错误"
            notify_renewal_failed(phone, "结果页", error_msg, bot_token, chat_id)
            return False

    except Exception as e:
        print(f"  ❌ 异常: {e}", flush=True)
        traceback.print_exc()
        if page:
            try:
                take_screenshot(page, f"error_{phone}.png", bot_token, chat_id, f"异常 - {phone}")
            except:
                pass
        notify_renewal_failed(phone, "执行异常", str(e), bot_token, chat_id)
        return False
    finally:
        if page:
            try:
                page.quit()
            except:
                pass

# ========== 主入口 ==========
if __name__ == "__main__":
    print("#########################")
    print("   Woiden 自动续期 (GitHub Actions 版)")
    print("#########################")
    if not ACCOUNTS:
        print("❌ 未加载账号，请设置 ACCOUNTS_JSON", flush=True)
        sys.exit(1)
    print(f"✅ 加载了 {len(ACCOUNTS)} 个账号", flush=True)
    success = 0
    for idx, acc in enumerate(ACCOUNTS, 1):
        print(f"\n===== 处理第 {idx}/{len(ACCOUNTS)} 个账号 =====", flush=True)
        try:
            if renew_account(acc):
                success += 1
        except Exception as e:
            print(f"  ⚠️ 账号处理异常: {e}", flush=True)
            traceback.print_exc()
        time.sleep(random.randint(10, 30))
    print(f"\n完成: {success}/{len(ACCOUNTS)} 个账号续期成功", flush=True)
