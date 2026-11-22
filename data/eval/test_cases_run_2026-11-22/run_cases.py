import json, sys, time, subprocess, os, pathlib
sp = pathlib.Path(sys.argv[1]); out = sp/"results.json"  # usage: python3 run_cases.py <katalog z cases.json i img/>
cases = json.load(open(sp/"cases.json", encoding="utf-8"))
res = json.load(open(out)) if out.exists() else {}
for c in cases:
    if c["id"] in res and res[c["id"]].get("http") == 200: continue
    for attempt in range(4):
        cmd = ["curl","-s","-m","180","-o",str(sp/"resp.json"),"-w","%{http_code} %{time_total}","-D",str(sp/"hdr.txt"),
               "--form-string",f"type={c['type']}","--form-string",f"channel={c.get('channel','web')}"]
        if c.get("text"): cmd += ["--form-string", f"text={c['text']}"]
        if c.get("url"): cmd += ["--form-string", f"url={c['url']}"]
        if c.get("file"): cmd += ["-F", f"file=@{sp/c['file']};type=image/png"]
        cmd.append("http://localhost:8001/check")
        code, t = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
        try: body = json.load(open(sp/"resp.json", encoding="utf-8"))
        except Exception: body = open(sp/"resp.json", errors="replace").read()
        if code in ("429","503"):
            ra = 60
            for l in open(sp/"hdr.txt"):
                if l.lower().startswith("retry-after:"): ra = int(l.split(":")[1].strip() or 60)
            print(c["id"], code, "retry in", ra, flush=True); time.sleep(min(ra,120)); continue
        break
    res[c["id"]] = {"input": c, "http": int(code), "seconds": float(t), "at": time.strftime("%Y-%m-%d %H:%M:%S"), "response": body}
    json.dump(res, open(out,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    v = [(x.get("verdict"), x.get("strength")) for x in body.get("claims",[])] if isinstance(body, dict) else body[:200]
    print(c["id"], code, t, body.get("status") if isinstance(body, dict) else "", v, flush=True)
print("DONE")
