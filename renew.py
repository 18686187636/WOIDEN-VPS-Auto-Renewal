#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Woiden VPS 自动续期（最终稳定版）
与 HAX 脚本逻辑一致：先读文件，若无则轮询 Telegram Bot API
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
from PIL import Image, ImageDraw, ImageFont
import urllib.request

# ========== 环境变量 ==========
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON", "[]")
ACCOUNTS = json.loads(ACCOUNTS_JSON)
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
PROXY_SERVER = os.getenv("PROXY_SERVER", "")
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

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
    if PROXY_SERVER.startswith(('http://', 'https://')):
        return {"http": PROXY_SERVER, "https": PROXY_SERVER}
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
            pass
    except Exception as e:
        print(f"  [截图] 失败: {e}", flush=True)

# ========== 续期码文件读写 ==========
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

# ========== 轮询 Telegram API 获取续期码（与 HAX 脚本完全一致） ==========
def get_renewal_code_from_telegram(bot_tokens, code_file, timeout=1800, poll_interval=10):
    """
    轮询 Telegram Bot API 获取续期码，同时检查文件。
    与 HAX 脚本的逻辑完全相同。
    """
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
                offsets[bt['token']] = max(u["update_id"] for u in data["result"]) + 1
            else:
                offsets[bt['token']] = 0
        except Exception as e:
            print(f"  [CODE] 获取偏移量失败 {bt['token'][-6:]}: {e}")
            offsets[bt['token']] = 0

    elapsed = 0
    while elapsed < timeout:
        # 每轮先检查文件（若文件有内容则立即返回）
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
                                # 写入文件缓存
                                write_code_to_file(code_file, code)
                                return code, bt.get("label", bt['token'][-6:])
            except Exception as e:
                print(f"  [CODE] 轮询异常 {bt['token'][-6:]}: {e}")

        time.sleep(poll_interval)
        elapsed += poll_interval
        if elapsed % 60 < poll_interval:
            print(f"  [CODE] 等待中... ({elapsed//60} 分钟)", flush=True)

    return "", None

# ========== 页面操作函数（完整保留） ==========
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
        except:
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
    except:
        pass
    return grid

def _fetch_image_bytes(page, url):
    import base64
    import json
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
            cookie_header = "; ".join(f"{c.name}={c.value}" for c in cookies if getattr(c, "name", None))
        except:
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
    except:
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
        except:
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
        except:
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

def close_ads(page):
    print("  [AD] 关闭广告...")
    page.wait(3)
    try:
        page.actions.press(Keys.ESCAPE).perform()
        page.wait(1)
    except:
        pass
    for keyword in ["Close", "close", "×", "关闭"]:
        try:
            el = page.ele(f'xpath://*[contains(text(), "{keyword}")]')
            if el and el.is_displayed:
                el.click_self()
                page.wait(1)
                break
        except:
            pass
    page.wait(3)
    js_remove = """
    (function() {
        var selectors = [
            '.overlay', '.modal-backdrop', '.popup-overlay',
            '[class*="overlay"]', '[class*="modal"]', '[class*="popup"]',
            '.ad-container', '.ad-wrapper', '.banner-ad'
        ];
        selectors.forEach(function(sel) {
            document.querySelectorAll(sel).forEach(function(el) { el.remove(); });
        });
        var all = document.querySelectorAll('*');
        all.forEach(function(el) {
            var style = getComputedStyle(el);
            if (style.position === 'fixed' && parseInt(style.zIndex) > 999) {
                el.remove();
            }
        });
    })();
    """
    try:
        page.run_js(js_remove)
        page.wait(1)
    except:
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
        except:
            continue
    if not ad_btn:
        print("未找到广告按钮")
        return True
    print("点击广告按钮...")
    try:
        ad_btn.click_self(by_js=True)
    except:
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

def _hard_set_value(page, value, *selectors):
    import json
    sel_json = json.dumps(list(selectors))
    val_js = value.replace("\\", "\\\\").replace("'", "\\'")
    js = """(function(v, sels){
var el=null;
for(var i=0;i<sels.length;i++){try{el=document.querySelector(sels[i]);}catch(e){el=null;}if(el)break;}
if(!el)return JSON.stringify({ok:false,reason:'NO_EL'});
var r=el.getBoundingClientRect();
var diag={id:el.id||'',name:el.name||'',cls:''+(el.className||''),ro:el.readOnly,dis:el.disabled,vis:el.offsetWidth>0&&el.offsetHeight>0,rect:Math.round(r.width)+'x'+Math.round(r.height),inForm:!!el.closest('form'),count:document.querySelectorAll(sels[0]).length};
try{el.scrollIntoView({block:'center'});}catch(e){}
try{el.focus();}catch(e){}
try{var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;setter.call(el,v);}catch(e){el.value=v;}
var afterSet=el.value;
el.dispatchEvent(new Event('input',{bubbles:true}));
var afterInput=el.value;
el.dispatchEvent(new Event('change',{bubbles:true}));
el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true}));
el.dispatchEvent(new Event('blur',{bubbles:true}));
var afterAll=el.value;
return JSON.stringify({ok:afterAll===v,reason:afterAll===v?'OK':'CHANGED',afterSet:afterSet,afterInput:afterInput,afterAll:afterAll,diag:diag});
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
        except:
            return False, '', 'PARSE_FAIL:%r' % res
    ok = bool(d.get('ok'))
    after = d.get('afterAll', '')
    return ok, after, ''

# ========== reCAPTCHA 相关函数 ==========
def find_frame(page, keyword):
    try:
        frames = page.get_frames()
        for frame in frames:
            frame_url = (frame.url or "").lower()
            if "recaptcha" in frame_url and keyword in frame_url:
                return frame
    except:
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
    except:
        pass
    anchor = find_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js(
                "(() => { try { const el = document.querySelector('#recaptcha-anchor'); return el ? (el.getAttribute('aria-checked') === 'true') : false; } catch(e) { return false; } })()"
            )
            if checked:
                return True
        except:
            pass
    return False

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
    page.actions.move_to(checkbox, duration=random.uniform(0.4, 1.0))
    time.sleep(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except:
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
    except:
        pass
    for _ in range(3):
        try:
            audio_btn = bframe.ele("#recaptcha-audio-button", timeout=3)
            if audio_btn:
                try:
                    audio_btn.click()
                except:
                    audio_btn.click(by_js=True)
                time.sleep(3)
                input_box = bframe.ele("#audio-response", timeout=1)
                if input_box and input_box.states.is_displayed:
                    return True
        except:
            pass
    try:
        bframe.run_js(
            "(() => { const btn = document.querySelector('#recaptcha-audio-button'); if (btn) btn.click(); })()"
        )
        time.sleep(3)
        input_box = bframe.ele("#audio-response", timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except:
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
        except:
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
        except:
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
            except:
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
    except:
        return False
    time.sleep(random.uniform(0.5, 1.5))
    try:
        verify_btn = bframe.ele("#recaptcha-verify-button", timeout=2)
        if verify_btn:
            try:
                verify_btn.click()
            except:
                verify_btn.click(by_js=True)
    except:
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
        except:
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

        # ---------- 登录 ----------
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
                except:
                    pass

            # Telegram OAuth
            iframe_xpath = "xpath://iframe[contains(@src, 'oauth.telegram.org')]"
            frame_found = False
            for _ in range(10):
                try:
                    if page.ele(iframe_xpath, timeout=2):
                        frame_found = True
                        break
                except:
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

        handle_ad_wall(page)
        if "woiden.id/vps-renew" not in page.url:
            page.get("https://woiden.id/vps-renew/")
            page.wait.doc_loaded(timeout=15)
            page.wait(3)

        # 域名输入强化
        print("  [FORM] 输入域名...")
        web_input = page.ele('css:#web_address')
        if web_input:
            try:
                page.actions.move_to(web_input, duration=0.5).click().pause(0.2).perform()
                page.wait(0.3)
                web_input.clear()
                for ch in "woiden.id":
                    web_input.input(ch)
                    time.sleep(0.05)
                page.run_js("document.querySelector('#web_address').blur();")
            except Exception as e:
                print(f"    鼠标输入失败: {e}，使用 JS 强制写入")
                ok, readback, _ = _hard_set_value(page, "woiden.id", '#web_address', 'input[name="web_address"]')
                if not ok:
                    print("    强制写入也失败，尝试直接 JS 赋值")
                    page.run_js("document.querySelector('#web_address').value = 'woiden.id';")
                    page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('input', {bubbles:true}));")
                    page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('change', {bubbles:true}));")

            readback = page.run_js("document.querySelector('#web_address').value") or ""
            if readback.strip() != "woiden.id":
                print(f"    ❌ 域名输入验证失败，当前值: '{readback}'，尝试重写...")
                page.run_js("document.querySelector('#web_address').value = 'woiden.id';")
                page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('input', {bubbles:true}));")
                page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('change', {bubbles:true}));")
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
                        page.actions.move_to(captcha_input).pause(0.2).click().pause(0.2).input(result).perform()
                    except:
                        captcha_input.input(result, clear=True)
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

        # 提交前再次检查域名
        final_domain = page.run_js("document.querySelector('#web_address').value") or ""
        if final_domain.strip() != "woiden.id":
            print(f"  ⚠️ 提交前域名仍不正确 ('{final_domain}')，强制修正")
            page.run_js("document.querySelector('#web_address').value = 'woiden.id';")
            page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('input', {bubbles:true}));")
            page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('change', {bubbles:true}));")

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
            except:
                resp_text = ""
            if resp_text:
                print(f"  [RESPONSE] 第{i+1}次检测: #response = '{resp_text[:80]}'")
                if "verification code has been sent" in resp_text.lower():
                    print("  [RESPONSE] ✅ 检测到 'verification code has been sent'")
                    found = True
                    break
                if "correct site address" in resp_text.lower():
                    print("  [RESPONSE] ❌ 服务器反馈域名错误，重新设置域名并重试...")
                    page.run_js("document.querySelector('#web_address').value = 'woiden.id';")
                    page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('input', {bubbles:true}));")
                    page.run_js("document.querySelector('#web_address').dispatchEvent(new Event('change', {bubbles:true}));")
                    renew_btn.click_self(by_js=True)
                    print("  [FORM] 重新点击 Renew VPS")
                    page.wait(5)
                    continue
            try:
                body_text = page.run_js("document.body.innerText") or ""
            except:
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
            except:
                body_text = ""
            if "verification code has been sent" in body_text.lower():
                resp_text = body_text
                found = True
            else:
                print(f"  [RESPONSE] 未检测到关键字，页面内容片段:\n{body_text[:500]}")
                try:
                    take_screenshot(page, f"no_response_{phone}.png", bot_token, chat_id, f"未检测到响应 - {phone}")
                except:
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

        # ---------- 获取续期码（与 HAX 脚本相同：文件优先 + 轮询回退） ----------
        print("  [CODE] 获取续期码...")
        # 清空文件（避免读到旧码）
        if os.path.exists(code_file):
            try:
                open(code_file, 'w').close()
            except:
                pass

        # 构建所有可用的 Bot Token 列表（用于轮询）
        all_bots = []
        seen = set()
        for acc in ACCOUNTS:
            t = acc.get("bot_token")
            if t and t not in seen:
                seen.add(t)
                all_bots.append({"token": t, "label": f"...{t[-6:]}"})
        if bot_token and bot_token not in seen:
            all_bots.insert(0, {"token": bot_token, "label": f"...{bot_token[-6:]}"})

        print(f"  [CODE] 共有 {len(all_bots)} 个 Bot Token 可供轮询")
        for bt in all_bots:
            print(f"    - {bt['label']}")

        # 先尝试读文件（可能已被转发器写入）
        TG_RENEW_CODE = read_code_from_file(code_file)
        if TG_RENEW_CODE:
            print(f"  [CODE] 从文件读取到续期码: {TG_RENEW_CODE[:20]}***")
        else:
            # 否则轮询 Telegram API
            code, src = get_renewal_code_from_telegram(
                all_bots, code_file,
                timeout=600,  # 10分钟
                poll_interval=5
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
            recaptcha_ok = is_recaptcha_solved(page)

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
