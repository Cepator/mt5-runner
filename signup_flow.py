import os, re, time, json, urllib.request

def log(m): print("[SIGNUP] " + str(m), flush=True)

EMAIL = os.environ.get("META_EMAIL", "rivet-trading@agentmail.to")
PASSWORD = os.environ["META_PASSWORD"]
MAILKEY = os.environ["AGENTMAIL_API_KEY"]

def http_json(url, key=None, timeout=30):
    req = urllib.request.Request(url)
    if key: req.add_header("Authorization", "Bearer " + key)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def wait_mail(subject_pat, tries=16, delay=15):
    for i in range(tries):
        try:
            d = http_json("https://api.agentmail.to/v0/inboxes/" + EMAIL + "/messages?limit=15", MAILKEY)
            msgs = d.get("messages", d if isinstance(d, list) else [])
            for m in msgs:
                subj = str(m.get("subject", ""))
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
    return None, ""

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page()
    log("STEP1 goto signup")
    pg.goto("https://app.metaapi.cloud/sign-up", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(2500)

    def fill(lbl, sel, val):
        for attempt in (lambda: pg.get_by_label(lbl, exact=False).first.fill(val),
                        lambda: pg.fill(sel, val)):
            try: attempt(); return True
            except Exception: continue
        return False

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
    log("STEP3 submit")
    try: pg.get_by_role("button", name=re.compile("submit", re.I)).click()
    except Exception: pg.locator("button[type=submit], button:has-text('Submit')").first.click()
    pg.wait_for_load_state("networkidle", timeout=60000)
    pg.wait_for_timeout(3000)
    log("STEP4 post-submit url " + pg.url)
    pg.screenshot(path="shot-after-submit.png")

    log("STEP5 wait verification email")
    subj, body = wait_mail(r"confirm|verif|metaapi|welcome")
    log("mail subject " + str(subj)[:80])
    link = None
    if body:
        links = re.findall(r"https?://[^\\s\"'<>]+", body.replace("\\u003d", "=").replace("\\u0026", "&"))
        links = [l for l in links if "metaapi" in l and not l.endswith((".png", ".css", ".js"))]
        link = links[0] if links else None
    log("verify link found " + str(bool(link)))
    if link:
        pg.goto(link, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(3000)
        log("STEP6 post-verify url " + pg.url)
        pg.screenshot(path="shot-after-verify.png")

    log("STEP7 find tokens page")
    pg.goto("https://app.metaapi.cloud/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(3000)
    clicked = False
    for t in ["API tokens", "Api tokens", "Tokens"]:
        try:
            pg.get_by_text(t, exact=False).first.click(); clicked = True; log("clicked nav " + t); break
        except Exception: continue
    pg.wait_for_timeout(2000)
    pg.screenshot(path="shot-tokens-page.png")

    token = ""
    log("STEP8 generate token")
    for t in ["Generate", "Add token", "Create token", "Add"]:
        try:
            pg.get_by_role("button", name=re.compile(t, re.I)).first.click()
            pg.wait_for_timeout(1500)
            try:
                pg.get_by_label(re.compile("name", re.I)).first.fill("rivet-agent")
            except Exception: pass
            pg.get_by_role("button", name=re.compile("generate|create|ok|save", re.I)).last.click()
            pg.wait_for_timeout(2500)
            break
        except Exception: continue
    pg.wait_for_timeout(2000)
    pg.screenshot(path="shot-token-created.png")
    body_txt = pg.content()
    m = re.findall(r"[A-Za-z0-9_\-\.]{30,}", body_txt)
    cands = [x for x in m if x.count(".") >= 2 or len(x) >= 40]
    if cands: token = max(cands, key=len)

    if token:
        open("meta_token.txt", "w").write(token)
        log("TOKEN_SAVED len " + str(len(token)))
    else:
        log("TOKEN_MISSING - page text dump follows")
        log(re.sub(r"<[^>]+>", " ", body_txt)[:600])
    b.close()
