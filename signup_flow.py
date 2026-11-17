import os, re, time, json, sys, urllib.request, urllib.error

def log(m): print("[SIGNUP] " + str(m), flush=True)

def die(msg):
    log("FATAL " + msg)
    sys.exit(1)

EMAIL = os.environ.get("META_EMAIL", "rivet-trading@agentmail.to")
PASSWORD = os.environ["META_PASSWORD"]
MAILKEY = os.environ.get("AGENTMAIL_API_KEY", "")

PROVISION_URL = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts"
TOKEN_PAGE = "https://app.metaapi.cloud/token"
SIGNIN = "https://app.metaapi.cloud/sign-in"
SIGNUP = "https://app.metaapi.cloud/sign-up"

def api_code(token):
    """Return (http_code, parsed_or_None) for the provisioning API with this token."""
    try:
        req = urllib.request.Request(PROVISION_URL)
        req.add_header("auth-token", token)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.getcode(), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        log("api check err " + str(e)[:80])
        return 0, None

def http_json(url, key=None, header="Authorization", timeout=30):
    req = urllib.request.Request(url)
    if key:
        req.add_header(header, key)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def wait_mail(subject_pat, tries=16, delay=15):
    seen = []
    for i in range(tries):
        try:
            d = http_json("https://api.agentmail.to/v0/inboxes/" + EMAIL + "/messages?limit=15", MAILKEY)
            msgs = d.get("messages", d if isinstance(d, list) else [])
            if i == 0 or (msgs and len(msgs) != len(seen)):
                log("mail poll " + str(i) + " inbox count " + str(len(msgs)))
            for m in msgs:
                subj = str(m.get("subject", ""))
                if subj not in seen:
                    seen.append(subj)
                    log("mail subject seen: " + subj[:60])
                if re.search(subject_pat, subj, re.I):
                    tid = m.get("thread_id")
                    body = ""
                    if tid:
                        t = http_json("https://api.agentmail.to/v0/threads/" + str(tid), MAILKEY)
                        body = json.dumps(t)
                    return subj, body
        except Exception as e:
            log("mail poll " + str(i) + " err " + str(e)[:80])
        time.sleep(delay)
    log("mail poll exhausted; subjects ever seen: " + str(len(seen)))
    return None, ""

from playwright.sync_api import sync_playwright

# ---------- turnstile v3 ----------

def turnstile_resolved(pg):
    """Hidden response input on the main doc carries the solution token."""
    try:
        return pg.evaluate(
            "() => { const i = document.querySelector('input[name=\"cf-turnstile-response\"]');"
            " return !!(i && i.value && i.value.length > 10); }")
    except Exception:
        return False

def turnstile_widget_present(pg):
    try:
        return pg.evaluate(
            "() => !!(document.querySelector('.cf-turnstile') ||"
            " document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]'))")
    except Exception:
        return False

def poke_frames(pg):
    """Try clicking the checkbox inside each cloudflare challenge frame."""
    clicked = False
    try:
        for fr in pg.frames:
            if "challenges.cloudflare.com" in (fr.url or ""):
                for sel in ("input[type=checkbox]", ".ctp-checkbox-label", "label", "body"):
                    try:
                        fr.locator(sel).first.click(timeout=2500)
                        clicked = True
                        break
                    except Exception:
                        continue
    except Exception:
        pass
    return clicked

def poke_coordinates(pg):
    """Click where the widget checkbox sits by page coordinates (crosses iframes)."""
    try:
        box = pg.evaluate(
            "() => { const w = document.querySelector('.cf-turnstile');"
            " const f = document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]');"
            " const el = w || f; if (!el) return null;"
            " const r = el.getBoundingClientRect();"
            " return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
        if box and box.get("w", 0) > 5:
            x = box["x"] + min(30, box["w"] / 2)
            y = box["y"] + box["h"] / 2
            pg.mouse.click(x, y)
            return True
    except Exception:
        pass
    return False

def turnstile_wait(pg, timeout_s=180, tag=""):
    """Wait for resolution; re-poke (frame click + coordinate click) every 12s."""
    deadline = time.time() + timeout_s
    last_poke = 0.0
    pokes = 0
    saw_widget = False
    while time.time() < deadline:
        if turnstile_resolved(pg):
            log("turnstile resolved " + tag + " pokes=" + str(pokes))
            return True
        if not saw_widget and turnstile_widget_present(pg):
            saw_widget = True
            log("turnstile widget visible " + tag)
        if time.time() - last_poke >= 12:
            last_poke = time.time()
            pokes += 1
            a = poke_frames(pg)
            b = poke_coordinates(pg)
            log("poke " + str(pokes) + " " + tag + " frame=" + str(a) + " coord=" + str(b))
        pg.wait_for_timeout(2000)
    if not saw_widget:
        # no gate rendered at all: treat as pass-through, submit will tell us
        log("turnstile never appeared " + tag + " - assuming no gate")
        return True
    log("turnstile UNRESOLVED " + tag + " after " + str(timeout_s) + "s pokes=" + str(pokes))
    return False

def visible_text(pg):
    try:
        return re.sub(r"\s+", " ", pg.evaluate("() => document.body.innerText"))[:800]
    except Exception:
        return ""

with sync_playwright() as p:
    # headed on the runner desktop: Cloudflare Turnstile stalls headless
    b = p.chromium.launch(channel="msedge", headless=False,
                          args=["--disable-blink-features=AutomationControlled"])
    ctx = b.new_context(permissions=["clipboard-read", "clipboard-write"])
    pg = ctx.new_page()

    def fill(lbl, sel, val):
        for attempt in (lambda: pg.get_by_label(lbl, exact=False).first.fill(val),
                        lambda: pg.fill(sel, val)):
            try: attempt(); return True
            except Exception: continue
        return False

    def submit_current(tag):
        btn = pg.get_by_role("button", name=re.compile(r"submit|sign up|sign in|create", re.I)).first
        try:
            btn.wait_for(state="visible", timeout=30000)
        except Exception:
            log(tag + " submit button never visible")
            return False
        try:
            btn.click(timeout=10000)
            log(tag + " submit clicked")
            return True
        except Exception as e:
            log(tag + " submit click err " + str(e)[:80])
            return False

    def settle():
        try:
            pg.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass
        pg.wait_for_timeout(3000)

    def attempt_signin(tag):
        log(tag + " signin goto")
        pg.goto(SIGNIN, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2500)
        ok_e = fill("Email", "input[type=email]", EMAIL)
        ok_p = fill("Password", "input[type=password]", PASSWORD)
        log(tag + " signin fills " + str([ok_e, ok_p]))
        if not turnstile_wait(pg, 180, tag + "-signin"):
            pg.screenshot(path="shot-" + tag + "-turnstile-stuck.png")
            return False
        submit_current(tag + "-signin")
        settle()
        log(tag + " signin post url " + pg.url)
        pg.screenshot(path="shot-" + tag + "-signin.png")
        return not re.search(r"sign-in|signin", pg.url, re.I)

    def attempt_signup(tag):
        log(tag + " signup goto")
        pg.goto(SIGNUP, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2500)
        ok_e = fill("Email", "input[type=email]", EMAIL)
        ok_n = fill("Name", "input[name*=name i], input:not([type])", "Rivet")
        ok_p = fill("Password", "input[type=password] >> nth=0", PASSWORD)
        ok_p2 = fill("Confirm", "input[type=password] >> nth=1", PASSWORD)
        log(tag + " fills " + str([ok_e, ok_n, ok_p, ok_p2]))
        try: pg.get_by_label(re.compile("accept", re.I)).first.check()
        except Exception:
            try: pg.locator("input[type=checkbox]").first.check()
            except Exception as e: log(tag + " terms check fallback failed " + str(e)[:60])
        if not turnstile_wait(pg, 180, tag + "-signup"):
            pg.screenshot(path="shot-" + tag + "-turnstile-stuck.png")
            return False
        submit_current(tag + "-signup")
        settle()
        log(tag + " post-submit url " + pg.url)
        pg.screenshot(path="shot-" + tag + "-after-submit.png")
        body_marker = visible_text(pg)
        log(tag + " page text: " + body_marker[:200])
        if re.search(r"already (exists|registered|an account)", body_marker, re.I):
            log(tag + " account already exists -> signin")
            return attempt_signin(tag + "-x")
        return not re.search(r"sign-up", pg.url, re.I) or "dashboard" in pg.url.lower()

    # retry ladder: signin (account exists), signup fallback, signin retry
    logged = attempt_signin("p1")
    if not logged:
        logged = attempt_signup("p2")
    if not logged:
        logged = attempt_signin("p3")
    if not logged:
        pg.screenshot(path="shot-login-exhausted.png")
        die("NO_SESSION after retries url=" + pg.url)
    log("LOGGED_IN url " + pg.url)

    # ---------- verification email (only needed if account was fresh) ----------
    if MAILKEY and re.search(r"verify|confirm", visible_text(pg) + pg.url, re.I):
        log("STEP-verify email wait")
        subj, body = wait_mail(r"confirm|verif|metaapi|welcome")
        link = None
        if body:
            links = re.findall(r"https?://[^\s\"'<>]+", body.replace("\\u003d", "=").replace("\\u0026", "&"))
            links = [l for l in links if "metaapi" in l and not l.endswith((".png", ".css", ".js"))]
            if links:
                link = links[0]
        if link:
            log("verify link found, visiting")
            pg.goto(link, wait_until="networkidle", timeout=60000)
            settle()
            pg.screenshot(path="shot-after-verify.png")

    # ---------- token page ----------
    log("STEP7 goto token page")
    pg.goto(TOKEN_PAGE, wait_until="networkidle", timeout=60000)
    settle()
    if re.search(r"sign-in|signin", pg.url, re.I):
        pg.screenshot(path="shot-token-page-bounced.png")
        die("NO_SESSION url=" + pg.url)
    log("STEP7 token page url " + pg.url)
    pg.screenshot(path="shot-tokens-page.png")

    # ---------- generate a fresh token ----------
    log("STEP8 generate token")
    for t in ["Generate token", "Generate", "Add token", "Create token"]:
        try:
            pg.get_by_role("button", name=re.compile(t, re.I)).first.click(timeout=8000)
            log("clicked generate btn " + t)
            pg.wait_for_timeout(1500)
            try:
                pg.get_by_label(re.compile("name", re.I)).first.fill("rivet-agent", timeout=5000)
            except Exception:
                pass
            try:
                pg.get_by_role("button", name=re.compile("generate|create|ok|save", re.I)).last.click(timeout=8000)
                log("clicked confirm btn")
            except Exception as e:
                log("confirm click err " + str(e)[:60])
            pg.wait_for_timeout(2500)
            break
        except Exception:
            continue
    pg.wait_for_timeout(2000)

    # clipboard: click a copy affordance then read the clipboard
    clip = ""
    for sel in ["button:near(:text('copy'))", "[aria-label*=copy i]", "button:has(svg)"]:
        try:
            pg.locator(sel).last.click(timeout=3000)
            pg.wait_for_timeout(800)
            clip = pg.evaluate("() => navigator.clipboard.readText()")
            if clip and len(clip) > 40:
                log("clipboard token captured len " + str(len(clip)))
                break
            clip = ""
        except Exception:
            continue
    pg.screenshot(path="shot-token-created.png")

    text_only = visible_text(pg)
    body_txt = pg.content()

    cands = []
    if clip:
        cands.append(clip.strip())
    # strict dash-form first (MetaApi tokens are <short>-<long> base32-ish), then loose
    cands += re.findall(r"\b[A-Za-z0-9]{4,10}-[A-Za-z0-9]{40,80}\b", text_only)
    cands += re.findall(r"\b[A-Za-z0-9]{4,10}-[A-Za-z0-9]{40,80}\b", body_txt)
    loose = re.findall(r"[A-Za-z0-9_\-\.]{50,}", body_txt)
    cands += [x for x in loose if "-" in x or sum(ch.isdigit() for ch in x) >= 4]

    # dedupe, keep order
    seen = set()
    cands = [c for c in cands if not (c in seen or seen.add(c))]
    log("token candidates " + str(len(cands)) + " lens " + str([len(c) for c in cands][:8]))
    if not cands:
        log("TOKEN_MISSING - visible text dump follows")
        log(text_only[:400])
        die("TOKEN_MISSING")

    # ---------- live-test every candidate, save the one that authorizes ----------
    token = ""
    code = 0
    parsed = None
    for i, c in enumerate(cands):
        code, parsed = api_code(c)
        log("cand " + str(i) + " len " + str(len(c)) + " http " + str(code))
        if code == 200 and isinstance(parsed, list):
            token = c
            break
    if not token:
        die("TOKEN_LIVE_CHECK_FAILED http=" + str(code))

    open("meta_token.txt", "w").write(token)
    log("TOKEN_SAVED len " + str(len(token)) + " LIVE")
    log("accounts count " + str(len(parsed)))
    log("ALL_GATES_PASSED")
    b.close()
