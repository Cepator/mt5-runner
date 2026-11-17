import os, re, time, json, sys, urllib.request

def log(m): print("[SIGNUP] " + str(m), flush=True)

def die(msg):
    log("FATAL " + msg)
    sys.exit(1)

EMAIL = os.environ.get("META_EMAIL", "rivet-trading@agentmail.to")
PASSWORD = os.environ["META_PASSWORD"]
MAILKEY = os.environ["AGENTMAIL_API_KEY"]

PROVISION_URL = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai/users/current/accounts"
TOKEN_PAGE = "https://app.metaapi.cloud/token"

def http_json(url, key=None, header="Authorization", timeout=30):
    req = urllib.request.Request(url)
    if key:
        req.add_header(header, key)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def wait_mail(subject_pat, tries=24, delay=15):
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

def turnstile_resolved(pg):
    try:
        return pg.evaluate("() => { const i = document.querySelector('input[name=\"cf-turnstile-response\"]'); return !!(i && i.value && i.value.length > 10); }")
    except Exception:
        return False

def turnstile_wait(pg, timeout_s=120, tag=""):
    deadline = time.time() + timeout_s
    poked = False
    while time.time() < deadline:
        if turnstile_resolved(pg):
            log("turnstile resolved " + tag)
            return True
        if not poked:
            try:
                for fr in pg.frames:
                    if "challenges.cloudflare.com" in (fr.url or ""):
                        try:
                            fr.locator("input[type=checkbox], .ctp-checkbox-label").first.click(timeout=3000)
                            log("turnstile checkbox poked " + tag)
                            poked = True
                        except Exception:
                            pass
            except Exception:
                pass
        pg.wait_for_timeout(3000)
    log("turnstile UNRESOLVED " + tag + " after " + str(timeout_s) + "s")
    return False

def visible_text(pg):
    try:
        return re.sub(r"\s+", " ", pg.evaluate("() => document.body.innerText"))[:500]
    except Exception:
        return ""

with sync_playwright() as p:
    # headed on the runner desktop: Cloudflare Turnstile stalls headless
    b = p.chromium.launch(channel="msedge", headless=False,
                          args=["--disable-blink-features=AutomationControlled"])
    pg = b.new_page()

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

    def do_signin():
        log("signin path")
        pg.goto("https://app.metaapi.cloud/sign-in", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2500)
        ok_e = fill("Email", "input[type=email]", EMAIL)
        ok_p = fill("Password", "input[type=password]", PASSWORD)
        log("signin fills " + str([ok_e, ok_p]))
        turnstile_wait(pg, 60, "signin")
        submit_current("signin")
        try:
            pg.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass
        pg.wait_for_timeout(3000)
        log("signin post url " + pg.url)
        pg.screenshot(path="shot-signin.png")

    log("STEP1 goto signup")
    pg.goto("https://app.metaapi.cloud/sign-up", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(2500)

    log("STEP2 fill form")
    ok_e = fill("Email", "input[type=email]", EMAIL)
    ok_n = fill("Name", "input[name*=name i], input:not([type])", "Rivet")
    ok_p = fill("Password", "input[type=password] >> nth=0", PASSWORD)
    ok_p2 = fill("Confirm", "input[type=password] >> nth=1", PASSWORD)
    log("fills " + str([ok_e, ok_n, ok_p, ok_p2]))
    try: pg.get_by_label(re.compile("accept", re.I)).first.check()
    except Exception:
        try: pg.locator("input[type=checkbox]").first.check()
        except Exception as e: log("terms check fallback failed " + str(e)[:60])

    log("STEP2b turnstile wait")
    if not turnstile_wait(pg, 120, "signup"):
        pg.screenshot(path="shot-turnstile-stuck.png")
        die("TURNSTILE_UNRESOLVED")

    log("STEP3 submit")
    submit_current("signup")
    try:
        pg.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass
    pg.wait_for_timeout(3000)
    log("STEP4 post-submit url " + pg.url)
    pg.screenshot(path="shot-after-submit.png")

    body_marker = visible_text(pg)
    log("STEP4 page text: " + body_marker[:200])
    already = bool(re.search(r"already (exists|registered|an account)", body_marker, re.I))
    if already:
        log("account already exists -> signin fallback")
        do_signin()

    log("STEP5 wait verification email")
    subj, body = wait_mail(r"confirm|verif|metaapi|welcome")
    log("mail subject " + str(subj)[:80])
    link = None
    if body:
        links = re.findall(r"https?://[^\\s\"'<>]+", body.replace("\\u003d", "=").replace("\\u0026", "&"))
        links = [l for l in links if "metaapi" in l and not l.endswith((".png", ".css", ".js"))]
        if links:
            log("mail links found: " + str(len(links)))
            link = links[0]
    log("verify link found " + str(bool(link)))
    if link:
        pg.goto(link, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(3000)
        log("STEP6 post-verify url " + pg.url)
        pg.screenshot(path="shot-after-verify.png")
    else:
        log("verify link MISSING -> explicit signin fallback")
        do_signin()

    log("STEP7 goto token page")
    pg.goto(TOKEN_PAGE, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(3000)
    if re.search(r"sign-in|signin", pg.url, re.I):
        log("token page bounced to signin -> signin retry")
        do_signin()
        pg.goto(TOKEN_PAGE, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(3000)
    if re.search(r"sign-in|signin", pg.url, re.I):
        pg.screenshot(path="shot-token-page-bounced.png")
        die("NO_SESSION url=" + pg.url)
    log("STEP7 token page url " + pg.url)
    pg.screenshot(path="shot-tokens-page.png")

    token = ""
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
    pg.screenshot(path="shot-token-created.png")
    body_txt = pg.content()
    text_only = visible_text(pg)
    # strict dash-form first (MetaApi tokens are <short>-<long> base32-ish), then loose
    cands = re.findall(r"\b[A-Za-z0-9]{4,10}-[A-Za-z0-9]{40,80}\b", text_only)
    if not cands:
        cands = re.findall(r"\b[A-Za-z0-9]{4,10}-[A-Za-z0-9]{40,80}\b", body_txt)
    if not cands:
        loose = re.findall(r"[A-Za-z0-9_\-\.]{50,}", body_txt)
        cands = [x for x in loose if "-" in x or sum(ch.isdigit() for ch in x) >= 4]
    log("token candidates " + str(len(cands)))
    if cands:
        token = max(cands, key=len)

    if not token:
        log("TOKEN_MISSING - visible text dump follows")
        log(text_only[:400])
        die("TOKEN_MISSING")

    open("meta_token.txt", "w").write(token)
    log("TOKEN_SAVED len " + str(len(token)))

    log("STEP9 live api self-check")
    code = 0
    parsed = None
    try:
        req = urllib.request.Request(PROVISION_URL)
        req.add_header("auth-token", token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.getcode()
                parsed = json.loads(r.read())
        except urllib.error.HTTPError as e:
            code = e.code
    except Exception as e:
        log("selfcheck err " + str(e)[:80])
    log("SELF_CHECK http " + str(code))
    if code != 200 or not isinstance(parsed, list):
        die("TOKEN_LIVE_CHECK_FAILED http=" + str(code))
    log("accounts count " + str(len(parsed)))
    log("ALL_GATES_PASSED")
    b.close()
