"""
DevAlert — 开发者远程工作告警服务
马斯克第一性原理：把免费的职位信息变成省时间的付费告警
"""

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import requests
import time
import json
import os
import secrets
import re
from collections import defaultdict
from datetime import datetime, timezone

app = FastAPI(
    title="DevAlert",
    description="Developer Remote Job Alerts — Get matched jobs delivered daily",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ 配置 ============

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(DATA_DIR, "users.json")
JOBS_CACHE_FILE = os.path.join(DATA_DIR, "jobs_cache.json")
PAYMENTS_FILE = os.path.join(DATA_DIR, "payments.json")
ADMIN_SECRET = "devalert-admin-2026"

BTC_ADDRESS = "1MLcB51Zya52oV445GZGMn1qqeYEAJ67Ds"
PAYPAL_ACCOUNT = "16666181244@163.com"

TIER_CONFIG = {
    "free": {"daily_limit": 3, "price": "$0/月", "btc_price": None, "paypal_amount": None},
    "pro": {"daily_limit": -1, "price": "$9/月", "btc_price": "0.0001 BTC", "paypal_amount": "$9"},
}

# ============ 数据持久化 ============

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                return json.load(f)
            except:
                return default
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

USERS = load_json(USERS_FILE, {})
PAYMENTS = load_json(PAYMENTS_FILE, [])

# ============ 职位爬取引擎 ============

def fetch_remoteok():
    """从 RemoteOK API 获取远程职位"""
    try:
        r = requests.get("https://remoteok.com/api", timeout=15,
                         headers={"User-Agent": "DevAlert/1.0"})
        if r.status_code != 200:
            return []
        data = r.json()
        jobs = []
        for item in data[1:]:  # 第一个是元数据
            if not isinstance(item, dict):
                continue
            tags = item.get("tags", []) or []
            job = {
                "id": f"rok_{item.get('id', '')}",
                "title": item.get("position", ""),
                "company": item.get("company", ""),
                "url": item.get("url", ""),
                "location": item.get("location", "Remote"),
                "tags": tags,
                "salary": item.get("salary_min", "") or "",
                "date": item.get("date", ""),
                "source": "RemoteOK",
                "type": item.get("type", ""),
                "description": (item.get("description", "") or "")[:500],
            }
            if job["title"] and job["url"]:
                jobs.append(job)
        return jobs
    except Exception as e:
        print(f"RemoteOK error: {e}")
        return []

def fetch_remotive(keywords=None):
    """从 Remotive API 获取远程开发职位"""
    try:
        url = "https://remotive.com/api/remote-jobs?category=software-dev&limit=100"
        if keywords:
            url += f"&search={keywords}"
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "DevAlert/1.0"})
        if r.status_code != 200:
            return []
        data = r.json()
        jobs = []
        for item in data.get("jobs", []):
            tags = item.get("tags", []) or []
            # Remotive tags 有时是 dict 列表
            tag_names = []
            for t in tags:
                if isinstance(t, dict):
                    tag_names.append(t.get("name", ""))
                else:
                    tag_names.append(str(t))
            job = {
                "id": f"rem_{item.get('id', '')}",
                "title": item.get("title", ""),
                "company": item.get("company_name", ""),
                "url": item.get("url", ""),
                "location": item.get("candidate_required_location", "Remote"),
                "tags": tag_names,
                "salary": item.get("salary", "") or "",
                "date": item.get("publication_date", ""),
                "source": "Remotive",
                "type": item.get("job_type", ""),
                "description": (item.get("description", "") or "")[:500],
            }
            if job["title"] and job["url"]:
                jobs.append(job)
        return jobs
    except Exception as e:
        print(f"Remotive error: {e}")
        return []

def fetch_hn_whos_hiring():
    """从 Hacker News 'Who is Hiring' 月帖获取职位"""
    try:
        # 找最新的月度帖
        for month in ["April", "March", "February", "January"]:
            r = requests.get(
                f"https://hn.algolia.com/api/v1/search?query=Ask+HN+Who+is+hiring+({month}+2026)&tags=story",
                timeout=15, headers={"User-Agent": "DevAlert/1.0"})
            hits = r.json().get("hits", [])
            story_id = None
            for h in hits:
                title = (h.get("title") or "").lower()
                if "who is hiring" in title and "2026" in title:
                    story_id = h["objectID"]
                    break
            if not story_id:
                continue

            # 获取评论
            r2 = requests.get(f"https://hn.algolia.com/api/v1/items/{story_id}",
                              timeout=20, headers={"User-Agent": "DevAlert/1.0"})
            data = r2.json()
            children = data.get("children", [])

            jobs = []
            for c in children[:200]:  # 最多200条评论
                text = c.get("text") or ""
                if not text or len(text) < 30:
                    continue
                # 解析HTML标签
                import re
                clean = re.sub(r'<[^>]+>', ' ', text)
                clean = re.sub(r'&#x2F;', '/', clean)
                clean = re.sub(r'&amp;', '&', clean)
                clean = re.sub(r'\s+', ' ', clean).strip()

                # 提取标题行（第一行通常包含公司名和职位）
                first_line = clean.split('.')[0].split('|')[0].strip()[:120]
                if not first_line or len(first_line) < 5:
                    continue

                # 提取链接
                urls = re.findall(r'https?://[^\s<"]+', text)
                job_url = urls[0] if urls else f"https://news.ycombinator.com/item?id={c.get('id','')}"

                # 提取远程标记
                is_remote = any(kw in clean.lower() for kw in ["remote", "anywhere", "work from home", "distributed"])

                job = {
                    "id": f"hn_{c.get('id', '')}",
                    "title": first_line,
                    "company": first_line.split('(')[0].strip()[:50] if '(' in first_line else first_line[:50],
                    "url": job_url,
                    "location": "Remote" if is_remote else "",
                    "tags": [],
                    "salary": "",
                    "date": c.get("created_at", ""),
                    "source": "HackerNews",
                    "type": "",
                    "description": clean[:500],
                }
                if is_remote or any(kw in clean.lower() for kw in ["engineer", "developer", "designer", "manager", "data", "product", "devops", "sre"]):
                    jobs.append(job)

            return jobs

        return []
    except Exception as e:
        print(f"HN error: {e}")
        return []

def fetch_all_jobs():
    """聚合所有数据源"""
    jobs = []
    seen_urls = set()

    for fetcher in [fetch_remoteok, fetch_remotive, fetch_hn_whos_hiring]:
        try:
            fetched = fetcher()
            for job in fetched:
                if job["url"] not in seen_urls:
                    seen_urls.add(job["url"])
                    jobs.append(job)
        except Exception as e:
            print(f"Fetcher error: {e}")

    # 按日期排序
    jobs.sort(key=lambda j: j.get("date", ""), reverse=True)

    # 缓存
    save_json(JOBS_CACHE_FILE, {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(jobs),
        "jobs": jobs,
    })

    return jobs

# ============ 关键词匹配 ============

def match_jobs(jobs, keywords, location_pref=None):
    """根据关键词和地点偏好过滤岗位"""
    if not keywords:
        return jobs[:50]

    keywords_lower = [k.lower().strip() for k in keywords if k.strip()]
    if not keywords_lower:
        return jobs[:50]

    matched = []
    for job in jobs:
        text = f"{job['title']} {job['company']} {' '.join(job['tags'])} {job.get('description','')}".lower()
        # 任一关键词匹配即入选
        if any(kw in text for kw in keywords_lower):
            # 地点过滤（可选）
            if location_pref:
                loc = (job.get("location") or "").lower()
                if location_pref.lower() not in loc and "remote" not in loc and "anywhere" not in loc:
                    continue
            matched.append(job)

    return matched

# ============ API 路由 ============

@app.get("/")
def landing():
    return HTMLResponse(content=LANDING_HTML)

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

@app.get("/api")
def api_info():
    return {
        "service": "DevAlert",
        "version": "1.0.0",
        "endpoints": [
            "GET /jobs — 浏览所有职位(需API Key)",
            "GET /jobs/match?keywords=python,remote — 关键词匹配",
            "POST /register — 注册",
            "POST /alerts — 设置告警关键词",
            "GET /alerts — 查看我的告警设置",
            "GET /check?email=... — 查看账户状态",
            "POST /confirm — 提交付款凭证",
            "POST /admin/approve — 管理员审批",
            "GET /admin/payments — 管理员查看付款",
        ],
        "docs": "/docs",
    }

# ---- 注册 ----

@app.post("/register")
def register(email: str = Query(..., description="你的邮箱")):
    global USERS
    USERS = load_json(USERS_FILE, {})

    email = email.lower().strip()
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        raise HTTPException(400, "邮箱格式不正确")

    # 检查是否已注册
    for uid, user in USERS.items():
        if user.get("email") == email:
            return {"status": "exists", "api_key": uid, "tier": user["tier"],
                    "message": "该邮箱已注册", "keywords": user.get("keywords", [])}

    # 生成新key
    api_key = f"da-{secrets.token_hex(8)}"
    USERS[api_key] = {
        "email": email,
        "tier": "free",
        "keywords": [],
        "location_pref": "",
        "created": time.time(),
    }
    save_json(USERS_FILE, USERS)

    return {
        "status": "created",
        "api_key": api_key,
        "tier": "free",
        "daily_limit": TIER_CONFIG["free"]["daily_limit"],
        "message": "注册成功！设置你的关键词开始接收告警",
    }

# ---- 设置告警 ----

@app.post("/alerts")
def set_alerts(
    api_key: str = Query(..., description="你的API Key"),
    keywords: str = Query(..., description="关键词，逗号分隔，如: python,react,remote"),
    location: str = Query("", description="地点偏好，如: us,eu,asia"),
):
    global USERS
    USERS = load_json(USERS_FILE, {})

    if api_key not in USERS:
        raise HTTPException(401, "无效API Key")

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        raise HTTPException(400, "至少输入一个关键词")

    # Free用户最多5个关键词
    user = USERS[api_key]
    if user["tier"] == "free" and len(kw_list) > 5:
        kw_list = kw_list[:5]

    user["keywords"] = kw_list
    user["location_pref"] = location.strip()
    save_json(USERS_FILE, USERS)

    return {
        "status": "updated",
        "keywords": kw_list,
        "location": location.strip(),
        "tier": user["tier"],
        "message": f"已设置 {len(kw_list)} 个关键词，每日推送匹配岗位",
    }

@app.get("/alerts")
def get_alerts(api_key: str = Query(..., description="你的API Key")):
    global USERS
    USERS = load_json(USERS_FILE, {})
    if api_key not in USERS:
        raise HTTPException(401, "无效API Key")
    user = USERS[api_key]
    return {
        "email": user["email"],
        "tier": user["tier"],
        "keywords": user.get("keywords", []),
        "location": user.get("location_pref", ""),
        "daily_limit": TIER_CONFIG[user["tier"]]["daily_limit"],
    }

# ---- 职位浏览 ----

@app.get("/jobs")
def list_jobs(api_key: str = Query(..., description="API Key")):
    global USERS
    USERS = load_json(USERS_FILE, {})
    if api_key not in USERS:
        raise HTTPException(401, "需要API Key。POST /register 注册")

    user = USERS[api_key]
    jobs = fetch_all_jobs()

    # Free用户限制
    if user["tier"] == "free":
        limit = TIER_CONFIG["free"]["daily_limit"]
        return {
            "count": len(jobs),
            "showing": min(limit, len(jobs)),
            "tier": "free",
            "upgrade": "Pro用户查看全部，POST /confirm 升级",
            "jobs": jobs[:limit],
        }

    return {"count": len(jobs), "jobs": jobs}

@app.get("/jobs/match")
def match_jobs_api(
    api_key: str = Query(..., description="API Key"),
    keywords: str = Query(..., description="关键词，逗号分隔"),
    location: str = Query("", description="地点偏好"),
):
    global USERS
    USERS = load_json(USERS_FILE, {})
    if api_key not in USERS:
        raise HTTPException(401, "需要API Key")

    user = USERS[api_key]
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    jobs = fetch_all_jobs()
    matched = match_jobs(jobs, kw_list, location if location else None)

    if user["tier"] == "free":
        limit = TIER_CONFIG["free"]["daily_limit"]
        return {
            "keywords": kw_list,
            "total_matched": len(matched),
            "showing": min(limit, len(matched)),
            "tier": "free",
            "upgrade": "Pro查看全部匹配",
            "jobs": matched[:limit],
        }

    return {"keywords": kw_list, "total_matched": len(matched), "jobs": matched}

# ---- 付款 ----

@app.post("/confirm")
async def confirm_payment(
    email: str = Query(..., description="你的邮箱"),
    payment_method: str = Query("paypal", description="btc 或 paypal"),
    transaction_id: str = Query("", description="PayPal交易号"),
    txid: str = Query("", description="BTC交易ID"),
):
    global PAYMENTS
    PAYMENTS = load_json(PAYMENTS_FILE, [])

    email = email.lower().strip()
    proof = transaction_id if payment_method == "paypal" else txid
    if not proof:
        raise HTTPException(400, "请提供交易号")

    # 检查重复
    for p in PAYMENTS:
        if p.get("proof") == proof and p.get("status") == "pending":
            return {"status": "already_submitted", "message": "该凭证已提交，无需重复提交"}

    record = {
        "email": email,
        "tier": "pro",
        "payment_method": payment_method,
        "proof": proof,
        "paypal_amount": "$9" if payment_method == "paypal" else None,
        "btc_amount": "0.0001 BTC" if payment_method == "btc" else None,
        "submitted_at": time.time(),
        "submitted_at_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
    }
    PAYMENTS.append(record)
    save_json(PAYMENTS_FILE, PAYMENTS)

    return {
        "status": "submitted",
        "message": "凭证已提交！验证后24小时内升级。",
        "details": {"email": email, "tier": "pro", "payment_method": payment_method, "amount": "$9"},
        "check_status": f"/check?email={email}",
    }

@app.get("/check")
def check_status(email: str = Query(...)):
    global USERS, PAYMENTS
    USERS = load_json(USERS_FILE, {})
    PAYMENTS = load_json(PAYMENTS_FILE, [])

    email = email.lower().strip()
    user_data = None
    for uid, user in USERS.items():
        if user.get("email") == email:
            user_data = {**user, "api_key": uid}
            break

    pending = [p for p in PAYMENTS if p.get("email") == email and p.get("status") == "pending"]

    if not user_data:
        return {"email": email, "status": "not_registered", "pending_payments": pending}

    return {
        "email": email,
        "status": "registered",
        "tier": user_data["tier"],
        "keywords": user_data.get("keywords", []),
        "daily_limit": TIER_CONFIG[user_data["tier"]]["daily_limit"],
        "pending_payments": pending,
    }

# ---- 管理员 ----

@app.get("/admin/payments")
def admin_payments(admin_key: str = Query(...)):
    if admin_key != ADMIN_SECRET:
        raise HTTPException(401, "无效管理员Key")
    PAYMENTS = load_json(PAYMENTS_FILE, [])
    pending = [p for p in PAYMENTS if p.get("status") == "pending"]
    verified = [p for p in PAYMENTS if p.get("status") == "verified"]
    return {
        "pending": pending, "pending_count": len(pending),
        "verified": verified, "verified_count": len(verified),
        "total_revenue": f"${len(verified) * 9}",
    }

@app.post("/admin/approve")
def admin_approve(email: str = Query(...), proof: str = Query(...), admin_key: str = Query(...)):
    if admin_key != ADMIN_SECRET:
        raise HTTPException(401, "无效管理员Key")

    global USERS, PAYMENTS
    USERS = load_json(USERS_FILE, {})
    PAYMENTS = load_json(PAYMENTS_FILE, [])

    # 找到pending记录
    target = None
    for p in PAYMENTS:
        if p.get("proof") == proof and p.get("email") == email and p.get("status") == "pending":
            target = p
            break
    if not target:
        raise HTTPException(404, "未找到该待审批记录")

    target["status"] = "verified"
    target["verified_at"] = time.time()

    # 升级用户
    for uid, user in USERS.items():
        if user.get("email") == email:
            user["tier"] = "pro"
            user["upgraded_at"] = time.time()
            save_json(USERS_FILE, USERS)
            save_json(PAYMENTS_FILE, PAYMENTS)
            return {"status": "approved", "email": email, "tier": "pro", "api_key": uid}

    # 用户不存在则自动创建
    api_key = f"da-{secrets.token_hex(8)}"
    USERS[api_key] = {
        "email": email,
        "tier": "pro",
        "keywords": [],
        "location_pref": "",
        "created": time.time(),
        "upgraded_at": time.time(),
    }
    save_json(USERS_FILE, USERS)
    save_json(PAYMENTS_FILE, PAYMENTS)
    return {"status": "approved", "email": email, "tier": "pro", "api_key": api_key, "note": "用户已自动创建"}

# ============ Landing Page ============

LANDING_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DevAlert — Remote Developer Jobs in Your Inbox</title>
<meta name="description" content="Get matched remote developer jobs delivered daily. Set your keywords, we do the searching. Free tier available.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
:root{--bg:#09090b;--bg-card:#111113;--border:#27272a;--border-hover:#3f3f46;--text:#fafafa;--text-sec:#a1a1aa;--text-muted:#71717a;--accent:#6366f1;--accent-lt:#818cf8;--accent-bg:rgba(99,102,241,.1);--green:#22c55e;--green-bg:rgba(34,197,94,.1);--red:#ef4444;--orange:#f59e0b}
*{margin:0;padding:0;box-sizing:border-box}html{scroll-behavior:smooth}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:var(--accent-lt);text-decoration:none}a:hover{text-decoration:underline}
nav{position:fixed;top:0;width:100%;z-index:100;background:rgba(9,9,11,.85);backdrop-filter:blur(12px);border-bottom:1px solid var(--border)}
.nav-in{max-width:1100px;margin:0 auto;padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between}
.nav-logo{font-weight:800;font-size:18px;display:flex;align-items:center;gap:8px}
.nav-links{display:flex;gap:24px;align-items:center}
.nav-links a{color:var(--text-sec);font-size:14px;font-weight:500}.nav-links a:hover{color:var(--text);text-decoration:none}
.nav-cta{padding:8px 18px;background:var(--accent);color:#fff!important;border-radius:8px;font-size:14px;font-weight:600}.nav-cta:hover{background:var(--accent-lt);text-decoration:none}
.hero{padding:130px 24px 70px;text-align:center;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:-200px;left:50%;transform:translateX(-50%);width:800px;height:500px;background:radial-gradient(ellipse,rgba(99,102,241,.12),transparent 70%);pointer-events:none}
.hero-badge{display:inline-flex;align-items:center;gap:6px;padding:6px 16px;background:var(--accent-bg);border:1px solid rgba(99,102,241,.3);border-radius:99px;font-size:13px;font-weight:600;color:var(--accent-lt);margin-bottom:24px}
.hero-badge .dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.hero h1{font-size:clamp(32px,5.5vw,58px);font-weight:900;letter-spacing:-.03em;line-height:1.1;margin-bottom:20px}
.hero h1 .grad{background:linear-gradient(135deg,#6366f1,#a855f7,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero-sub{font-size:17px;color:var(--text-sec);max-width:560px;margin:0 auto 32px}
.hero-form{max-width:460px;margin:0 auto;display:flex;gap:8px}
.hero-form input{flex:1;padding:13px 16px;border-radius:10px;border:1px solid var(--border);background:var(--bg-card);color:var(--text);font-size:15px}
.hero-form input:focus{outline:none;border-color:var(--accent)}
.hero-form button{padding:13px 24px;background:var(--accent);color:#fff;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;white-space:nowrap}
.hero-form button:hover{background:var(--accent-lt)}
.hero-stats{display:flex;gap:40px;justify-content:center;margin-top:48px;flex-wrap:wrap}
.hero-stat .num{font-size:28px;font-weight:800}.hero-stat .lbl{font-size:12px;color:var(--text-muted);margin-top:2px}
.sec{padding:72px 24px;max-width:1100px;margin:0 auto}
.sec-t{font-size:32px;font-weight:800;text-align:center;margin-bottom:8px;letter-spacing:-.02em}
.sec-d{text-align:center;color:var(--text-sec);max-width:500px;margin:0 auto 44px;font-size:15px}
.how-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;max-width:800px;margin:0 auto}
.how-card{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:28px;text-align:center;transition:all .2s}
.how-card:hover{border-color:var(--border-hover);transform:translateY(-2px)}
.how-num{width:36px;height:36px;border-radius:50%;background:var(--accent);color:#fff;display:inline-flex;align-items:center;justify-content:center;font-weight:800;font-size:16px;margin-bottom:12px}
.how-card h3{font-size:16px;font-weight:700;margin-bottom:6px}.how-card p{font-size:14px;color:var(--text-sec)}
.jobs-preview{background:var(--bg-card);border:1px solid var(--border);border-radius:16px;overflow:hidden;max-width:700px;margin:0 auto}
.jp-header{padding:16px 20px;border-bottom:1px solid var(--border);font-weight:700;font-size:14px;display:flex;justify-content:space-between;align-items:center}
.jp-header .badge{background:var(--green-bg);color:var(--green);padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600}
.jp-item{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;transition:background .1s;cursor:pointer}
.jp-item:last-child{border-bottom:none}
.jp-item:hover{background:rgba(255,255,255,.02)}
.jp-title{font-size:14px;font-weight:600}.jp-meta{font-size:12px;color:var(--text-muted);margin-top:2px}
.jp-tags{display:flex;gap:4px;flex-wrap:wrap}.jp-tag{padding:2px 8px;background:var(--accent-bg);color:var(--accent-lt);border-radius:4px;font-size:11px;font-weight:500}
.prg{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;max-width:640px;margin:0 auto;align-items:start}
.pc{background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:32px;position:relative;transition:all .2s}
.pc:hover{border-color:var(--border-hover)}
.pc.feat{border-color:var(--accent);box-shadow:0 0 40px rgba(99,102,241,.08)}
.pc.feat::before{content:'Most Popular';position:absolute;top:-13px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:4px 14px;border-radius:99px;font-size:11px;font-weight:700}
.pc .pn{font-size:13px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em}
.pc .pa{font-size:44px;font-weight:900;margin:8px 0 4px;letter-spacing:-.03em}.pc .pa span{font-size:15px;font-weight:400;color:var(--text-muted)}
.pc .pd{font-size:14px;color:var(--text-sec);margin-bottom:18px}
.pc ul{list-style:none;margin-bottom:20px}.pc li{font-size:14px;color:var(--text-sec);padding:4px 0;display:flex;align-items:flex-start;gap:8px}
.pc li::before{content:'\\2713';color:var(--green);font-weight:700;flex-shrink:0}
.pc .pb{display:block;text-align:center;padding:12px;border-radius:10px;font-weight:600;font-size:14px;cursor:pointer;border:none;width:100%;transition:all .15s}
.pb-p{background:var(--accent);color:#fff}.pb-p:hover{background:var(--accent-lt)}
.pb-s{background:transparent;color:var(--text);border:1px solid var(--border)}.pb-s:hover{border-color:var(--border-hover)}
.paybox{margin-top:10px;padding:14px;background:rgba(0,0,0,.2);border-radius:10px;border:1px solid var(--border)}
.paybox .pl{font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;margin-bottom:4px}
.paybox .pv{font-family:monospace;font-size:11px;color:var(--text-sec);background:var(--bg);padding:6px 8px;border-radius:6px;word-break:break-all}
.faq{max-width:640px;margin:0 auto}
.faq-i{border:1px solid var(--border);border-radius:10px;margin-bottom:6px;overflow:hidden}.faq-i:hover{border-color:var(--border-hover)}
.faq-q{padding:16px 18px;cursor:pointer;font-weight:600;font-size:14px;display:flex;justify-content:space-between;align-items:center;user-select:none}
.faq-q::after{content:'+';color:var(--text-muted);font-size:18px;transition:transform .2s}.faq-i.open .faq-q::after{transform:rotate(45deg)}
.faq-a{padding:0 18px 16px;font-size:13px;color:var(--text-sec);display:none;line-height:1.6}.faq-i.open .faq-a{display:block}
footer{border-top:1px solid var(--border);padding:32px 24px;text-align:center}
footer p{color:var(--text-muted);font-size:12px}footer .links{display:flex;gap:20px;justify-content:center;margin-bottom:12px}footer .links a{color:var(--text-muted);font-size:12px}
.mo{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);z-index:1000;justify-content:center;align-items:center}
.mo.active{display:flex}
.md{background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:32px;max-width:440px;width:90%;position:relative;animation:mIn .2s ease}
@keyframes mIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.md h3{font-size:18px;font-weight:700;margin-bottom:18px}
.md label{display:block;font-size:12px;color:var(--text-muted);margin:12px 0 4px;font-weight:500}
.md input,.md select{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:14px}
.md input:focus,.md select:focus{outline:none;border-color:var(--accent)}
.md-btn{width:100%;padding:11px;border-radius:8px;border:none;font-weight:700;font-size:14px;cursor:pointer;margin-top:16px;background:var(--accent);color:#fff}.md-btn:hover{background:var(--accent-lt)}
.md-x{position:absolute;top:14px;right:14px;background:none;border:none;color:var(--text-muted);font-size:16px;cursor:pointer}
.md .msg{margin-top:12px;padding:10px;border-radius:8px;font-size:12px;display:none;line-height:1.5}
.md .msg.ok{display:block;background:var(--green-bg);color:var(--green);border:1px solid rgba(34,197,94,.2)}
.md .msg.err{display:block;background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.2)}
@media(max-width:768px){.hero{padding:110px 16px 50px}.hero h1{font-size:28px}.hero-form{flex-direction:column}.hero-stats{gap:20px}.sec{padding:48px 16px}.nav-links{display:none}.prg{grid-template-columns:1fr}}
</style>
</head>
<body>
<nav><div class="nav-in">
  <div class="nav-logo">\\U0001f514 DevAlert</div>
  <div class="nav-links">
    <a href="#how">How it Works</a><a href="#jobs">Live Jobs</a><a href="#pricing">Pricing</a><a href="#faq">FAQ</a>
    <a href="#" class="nav-cta" onclick="openModal('register');return false">Sign Up Free</a>
  </div>
</div></nav>

<div class="hero">
  <div class="hero-badge"><span class="dot"></span> 150+ remote jobs updated hourly</div>
  <h1>Stop searching.<br><span class="grad">Let jobs find you.</span></h1>
  <p class="hero-sub">Set your keywords once. Get matched remote developer jobs in your inbox every morning. No more endless scrolling.</p>
  <div class="hero-form">
    <input type="email" id="heroEmail" placeholder="you@email.com">
    <button onclick="quickSignup()">Get Free Alerts</button>
  </div>
  <div class="hero-stats">
    <div class="hero-stat"><div class="num">150+</div><div class="lbl">Daily Jobs</div></div>
    <div class="hero-stat"><div class="num">5</div><div class="lbl">Data Sources</div></div>
    <div class="hero-stat"><div class="num">2min</div><div class="lbl">Setup Time</div></div>
  </div>
</div>

<div class="sec" id="how">
  <div class="sec-t">How it works</div>
  <div class="sec-d">3 steps. 2 minutes. Done.</div>
  <div class="how-grid">
    <div class="how-card"><div class="how-num">1</div><h3>Set Keywords</h3><p>Tell us what you're looking for: React, Python, DevOps, remote, EU...</p></div>
    <div class="how-card"><div class="how-num">2</div><h3>We Search</h3><p>We scan 150+ remote jobs daily from RemoteOK, Remotive, and more.</p></div>
    <div class="how-card"><div class="how-num">3</div><h3>Get Matched</h3><p>Only jobs matching YOUR keywords land in your inbox. Zero noise.</p></div>
  </div>
</div>

<div class="sec" id="jobs" style="padding-top:0">
  <div class="sec-t">Live jobs right now</div>
  <div class="sec-d">Fresh from the source. Updated hourly.</div>
  <div class="jobs-preview">
    <div class="jp-header"><span>Latest Remote Dev Jobs</span><span class="badge" id="jobCount">Loading...</span></div>
    <div id="jobList"><div style="padding:20px;text-align:center;color:var(--text-muted)">Loading...</div></div>
  </div>
</div>

<div class="sec" id="pricing" style="padding-top:0">
  <div class="sec-t">Simple pricing</div>
  <div class="sec-d">Start free. Upgrade when you need more.</div>
  <div class="prg">
    <div class="pc">
      <div class="pn">Free</div><div class="pa">$0<span>/mo</span></div><div class="pd">3 matched jobs/day &middot; 5 keywords max</div>
      <ul><li>Daily email digest</li><li>5 keywords</li><li>3 job matches per day</li><li>Browse live jobs</li></ul>
      <button class="pb pb-s" onclick="openModal('register')">Get Started</button>
    </div>
    <div class="pc feat">
      <div class="pn">Pro</div><div class="pa">$9<span>/mo</span></div><div class="pd">Unlimited matches &middot; Unlimited keywords</div>
      <ul><li>Unlimited job matches</li><li>Unlimited keywords</li><li>Location preferences</li><li>Priority alerts</li></ul>
      <div class="paybox"><div class="pl">\\u20bf BTC</div><div class="pv">1MLcB51Zya52oV445GZGMn1qqeYEAJ67Ds</div><div class="pl" style="margin-top:8px">\\U0001f4b3 PayPal</div><div class="pv">16666181244@163.com &mdash; $9</div></div>
      <button class="pb pb-p" onclick="openModal('pay')">I've Paid &rarr;</button>
    </div>
  </div>
</div>

<div class="sec" id="faq" style="padding-top:0">
  <div class="sec-t">FAQ</div>
  <div class="faq">
    <div class="faq-i" onclick="this.classList.toggle('open')"><div class="faq-q">What job sources do you scan?</div><div class="faq-a">We aggregate from RemoteOK, Remotive, and other remote job boards. New sources added regularly.</div></div>
    <div class="faq-i" onclick="this.classList.toggle('open')"><div class="faq-q">How often are jobs updated?</div><div class="faq-a">We fetch fresh jobs every hour. Your daily email arrives each morning with the latest matches.</div></div>
    <div class="faq-i" onclick="this.classList.toggle('open')"><div class="faq-q">Can I set location preferences?</div><div class="faq-a">Pro users can filter by region (US, EU, Asia, etc). Free users see all remote jobs matching their keywords.</div></div>
    <div class="faq-i" onclick="this.classList.toggle('open')"><div class="faq-q">How does payment work?</div><div class="faq-a">BTC or PayPal. Send payment, submit your transaction ID, and we upgrade you within 24 hours.</div></div>
  </div>
</div>

<footer>
  <div class="links"><a href="/docs">API</a><a href="https://github.com/ssaassaasas/finapi-gateway" target="_blank">GitHub</a></div>
  <p>&copy; 2026 DevAlert &middot; Remote jobs, zero noise</p>
</footer>

<div class="mo" id="regMo" onclick="if(event.target===this)this.classList.remove('active')">
  <div class="md"><button class="md-x" onclick="document.getElementById('regMo').classList.remove('active')">\\u2715</button>
  <h3>Sign Up for Free Alerts</h3>
  <label>Email</label><input type="email" id="regEmail" placeholder="you@email.com">
  <label>Keywords (comma separated)</label><input type="text" id="regKeywords" placeholder="react, python, remote, devops">
  <button class="md-btn" onclick="doReg()">Create Alert</button>
  <div class="msg" id="regMsg"></div></div>
</div>

<div class="mo" id="payMo" onclick="if(event.target===this)this.classList.remove('active')">
  <div class="md"><button class="md-x" onclick="document.getElementById('payMo').classList.remove('active')">\\u2715</button>
  <h3>Confirm Payment</h3>
  <label>Email</label><input type="email" id="payEmail" placeholder="you@email.com">
  <label>Payment Method</label><select id="payMethod" onchange="swPM()"><option value="paypal">\\U0001f4b3 PayPal</option><option value="btc">\\u20bf Bitcoin</option></select>
  <div id="ppF"><label>PayPal Transaction ID</label><input type="text" id="ppTxn" placeholder="e.g. 7XG46354GB5183632"></div>
  <div id="btcF" style="display:none"><label>BTC Transaction ID</label><input type="text" id="btcTxid" placeholder="e.g. a1b2c3d4..."></div>
  <button class="md-btn" onclick="doPay()">Submit Proof</button>
  <div class="msg" id="payMsg"></div></div>
</div>

<script>
async function loadJobs(){try{const r=await fetch('/jobs?api_key=da-preview');const d=await r.json();const jobs=d.jobs||[];document.getElementById('jobCount').textContent=jobs.length+' jobs';let html='';jobs.slice(0,8).forEach(j=>{const tags=(j.tags||[]).slice(0,3).map(t=>'<span class="jp-tag">'+t+'</span>').join('');html+='<div class="jp-item"><div><div class="jp-title">'+j.title+'</div><div class="jp-meta">'+j.company+(j.location?' &middot; '+j.location:'')+'</div></div><div class="jp-tags">'+tags+'</div></div>'});document.getElementById('jobList').innerHTML=html||'<div style="padding:20px;text-align:center;color:var(--text-muted)">No jobs found</div>'}catch(e){document.getElementById('jobList').innerHTML='<div style="padding:20px;text-align:center;color:var(--text-muted)">Error loading jobs</div>'}}
loadJobs();
function openModal(t){if(t==='register')document.getElementById('regMo').classList.add('active');else document.getElementById('payMo').classList.add('active')}
function quickSignup(){const e=document.getElementById('heroEmail').value.trim();if(e){document.getElementById('regEmail').value=e;openModal('register')}}
function swPM(){const m=document.getElementById('payMethod').value;document.getElementById('ppF').style.display=m==='paypal'?'block':'none';document.getElementById('btcF').style.display=m==='btc'?'block':'none'}
async function doReg(){const email=document.getElementById('regEmail').value.trim();const kw=document.getElementById('regKeywords').value.trim();const msg=document.getElementById('regMsg');if(!email||!email.includes('@')){msg.className='msg err';msg.textContent='Enter a valid email';return}msg.className='msg';msg.style.display='none';try{let r=await fetch('/register?email='+encodeURIComponent(email),{method:'POST'});let d=await r.json();if(d.api_key){if(kw){r=await fetch('/alerts?api_key='+d.api_key+'&keywords='+encodeURIComponent(kw),{method:'POST'});d=await r.json()}const el=document.getElementById('regEmail');msg.className='msg ok';msg.innerHTML='\\u2705 Done! API Key: <strong style="color:#fff;font-family:monospace">'+(d.api_key||'check email')+'</strong><br>Alerts set for: '+(kw||'none')}else{msg.className='msg err';msg.textContent=d.detail||d.message||'Failed'}}catch(e){msg.className='msg err';msg.textContent='Network error'}}
async function doPay(){const email=document.getElementById('payEmail').value.trim();const method=document.getElementById('payMethod').value;const proof=method==='paypal'?document.getElementById('ppTxn').value.trim():document.getElementById('btcTxid').value.trim();const msg=document.getElementById('payMsg');if(!email||!email.includes('@')){msg.className='msg err';msg.textContent='Enter a valid email';return}if(!proof){msg.className='msg err';msg.textContent='Enter transaction ID';return}msg.className='msg';msg.style.display='none';try{let url='/confirm?email='+encodeURIComponent(email)+'&payment_method='+method;if(method==='paypal')url+='&transaction_id='+encodeURIComponent(proof);else url+='&txid='+encodeURIComponent(proof);const r=await fetch(url,{method:'POST'});const d=await r.json();if(d.status==='submitted'){msg.className='msg ok';msg.innerHTML='\\u2705 Submitted! We\\'ll upgrade you within 24h.'}else if(d.status==='already_submitted'){msg.className='msg ok';msg.innerHTML='\\u2139\\ufe0f Already submitted'}else{msg.className='msg err';msg.textContent=d.detail||'Failed'}}catch(e){msg.className='msg err';msg.textContent='Network error'}}
</script>
</body>
</html>
"""

# ============ 预览端点（无需key查看岗位列表） ============

# 特殊key用于首页预览
USERS["da-preview"] = {"email": "preview@devalert.io", "tier": "pro", "keywords": [], "location_pref": "", "created": time.time()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
