"""
GitHub Actions worker. Supabase schema বদলায় না।

user_x_accounts কলাম:
  username=X username | api_key=password
  access_token=auth_token কুকি | access_token_secret=ct0 কুকি

দুই মোড:
 1) SINGLE (TWEET_ID দেওয়া): ১টা tweet, ১টা account। শুধু post_link / FAILED DB তে লেখে
    (Render এর পুরনো কোড বাকিটা সামলায়: scheduler, slot ইত্যাদি)
 2) BATCH (TWEET_ID ফাঁকা): এই account এর সব tweet (account_username=X, status=PROCESSING)
    একটার পর একটা পোস্ট করে, প্রতিটা সাথে সাথে DB তে POSTED + লিংক লেখে,
    Render কে জানায় (link bot + summary)।
"""
import os, sys, asyncio, random, traceback, datetime
from urllib.parse import quote
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
TG_ID = os.environ["TG_ID"]
USERNAME = os.environ["X_USERNAME"].strip().lower().lstrip("@")
TWEET_ID = os.environ.get("TWEET_ID", "").strip()
RENDER_URL = os.environ.get("RENDER_URL", "").rstrip("/")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
GAP_MIN = float(os.environ.get("POST_GAP_MIN", "12"))
GAP_MAX = float(os.environ.get("POST_GAP_MAX", "22"))
MAX_CONSEC_FAIL = 3

HEAD = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": "application/json"}
HOME_SEL = '[data-testid="SideNav_AccountSwitcher_Button"]'


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def sb_get(path):
    r = requests.get(f"{SB_URL}/rest/v1/{path}", headers=HEAD, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_patch(path, data):
    r = requests.patch(f"{SB_URL}/rest/v1/{path}", headers=HEAD, json=data, timeout=20)
    r.raise_for_status()


def notify(kind, **kw):
    if not RENDER_URL or not WORKER_SECRET:
        return
    try:
        requests.post(f"{RENDER_URL}/internal/worker_event",
                      headers={"X-Worker-Secret": WORKER_SECRET},
                      json={"type": kind, "tg_id": TG_ID, "username": USERNAME, **kw},
                      timeout=25)
    except Exception as e:
        print("notify failed:", e)


def save_cookies(cookies):
    auth = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
    ct0 = next((c["value"] for c in cookies if c["name"] == "ct0"), None)
    if auth and ct0:
        sb_patch(f"user_x_accounts?tg_id=eq.{TG_ID}&username=eq.{quote(USERNAME)}",
                 {"access_token": auth, "access_token_secret": ct0})


async def is_logged_in(page):
    await page.goto("https://x.com/home", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(HOME_SEL, timeout=12000)
        return True
    except PWTimeout:
        return False


async def login(page, password):
    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
    await page.wait_for_selector('input[autocomplete="username"]', timeout=20000)
    await page.fill('input[autocomplete="username"]', USERNAME)
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_selector('input[name="password"]', timeout=8000)
    except PWTimeout:
        extra = page.locator('input[data-testid="ocfEnterTextTextInput"]')
        if await extra.count():
            await extra.fill(USERNAME)
            await page.keyboard.press("Enter")
            await page.wait_for_selector('input[name="password"]', timeout=10000)
        else:
            raise RuntimeError("Login blocked (captcha/verification). Use saved cookies.")
    await page.fill('input[name="password"]', password)
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_selector(HOME_SEL, timeout=25000)
    except PWTimeout:
        raise RuntimeError("Login failed (wrong password, 2FA or verification).")


async def post(page, text):
    await page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
    box = page.locator('div[data-testid="tweetTextarea_0"]')
    await box.wait_for(timeout=20000)
    await box.click()
    await page.keyboard.insert_text(text)
    btn = page.locator('button[data-testid="tweetButton"]')
    async with page.expect_response(
        lambda r: "CreateTweet" in r.url and r.request.method == "POST", timeout=30000
    ) as ri:
        await btn.click()
    data = await (await ri.value).json()
    try:
        rest_id = data["data"]["create_tweet"]["tweet_results"]["result"]["rest_id"]
    except Exception:
        msg = (data.get("errors") or [{}])[0].get("message", "CreateTweet failed")
        raise RuntimeError(msg)
    return f"https://x.com/{USERNAME}/status/{rest_id}"


async def open_session(p):
    acc = sb_get(f"user_x_accounts?tg_id=eq.{TG_ID}&username=eq.{quote(USERNAME)}"
                 f"&select=api_key,access_token,access_token_secret&limit=1")
    if not acc:
        raise RuntimeError("Account not found in DB")
    acc = acc[0]
    browser = await p.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 900}, locale="en-US",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
    base = {"domain": ".x.com", "path": "/", "secure": True, "httpOnly": True}
    if acc.get("access_token") and acc.get("access_token_secret"):
        await ctx.add_cookies([
            {**base, "name": "auth_token", "value": acc["access_token"]},
            {**base, "name": "ct0", "value": acc["access_token_secret"]}])
    page = await ctx.new_page()
    if not await is_logged_in(page):
        if not acc.get("api_key"):
            raise RuntimeError("No saved session and no password.")
        await login(page, acc["api_key"])
        save_cookies(await ctx.cookies())
    return browser, ctx, page


async def run_single():
    rows = sb_get(f"scheduled_tweets?id=eq.{TWEET_ID}&select=tweet_text&limit=1")
    if not rows:
        raise RuntimeError("Tweet not found in DB")
    async with async_playwright() as p:
        browser, ctx, page = await open_session(p)
        link = await post(page, rows[0]["tweet_text"])
        save_cookies(await ctx.cookies())
        await browser.close()
    sb_patch(f"scheduled_tweets?id=eq.{TWEET_ID}", {"post_link": link})
    print("OK", link)


def reset_pending(ids):
    if not ids:
        return
    sb_patch(f"scheduled_tweets?id=in.({','.join(map(str, ids))})",
             {"post_status": "PENDING", "account_username": None})


async def run_batch():
    tweets = sb_get(f"scheduled_tweets?tg_id=eq.{TG_ID}&account_username=eq.{quote(USERNAME)}"
                    f"&post_status=eq.PROCESSING&order=id.asc&select=id,tweet_text")
    if not tweets:
        print("No queued tweets for", USERNAME)
        return
    posted = failed = consec = 0
    left_ids, error = [], None
    async with async_playwright() as p:
        try:
            browser, ctx, page = await open_session(p)
        except Exception as e:
            reset_pending([t["id"] for t in tweets])
            notify("summary", posted=0, failed=0, left=len(tweets), error=str(e))
            raise
        for i, t in enumerate(tweets):
            try:
                link = await post(page, t["tweet_text"])
                sb_patch(f"scheduled_tweets?id=eq.{t['id']}",
                         {"post_status": "POSTED", "post_link": link,
                          "account_username": USERNAME, "posted_at": now_iso()})
                notify("posted", link=link)
                posted += 1
                consec = 0
                print(f"[{i+1}/{len(tweets)}] OK {link}")
            except Exception as e:
                failed += 1
                consec += 1
                error = str(e)
                print(f"[{i+1}/{len(tweets)}] FAIL {e}")
                sb_patch(f"scheduled_tweets?id=eq.{t['id']}",
                         {"post_status": "FAILED", "account_username": USERNAME})
                if consec >= MAX_CONSEC_FAIL:
                    left_ids = [x["id"] for x in tweets[i + 1:]]
                    break
            if i < len(tweets) - 1:
                await asyncio.sleep(random.uniform(GAP_MIN, GAP_MAX))
        try:
            save_cookies(await ctx.cookies())
        except Exception:
            pass
        await browser.close()
    reset_pending(left_ids)
    notify("summary", posted=posted, failed=failed, left=len(left_ids),
           error=error if (failed or left_ids) else None)


if __name__ == "__main__":
    try:
        if TWEET_ID:
            asyncio.run(run_single())
        else:
            asyncio.run(run_batch())
    except Exception as e:
        print("ERROR:", e)
        traceback.print_exc()
        if TWEET_ID:
            try:
                sb_patch(f"scheduled_tweets?id=eq.{TWEET_ID}", {"post_status": "FAILED"})
            except Exception:
                pass
        sys.exit(1)
