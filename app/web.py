"""Servicio web: lee la BD del colector, calcula tabla y alertas, sirve la web (puerto de race.cfg)."""
import json, os, sqlite3, statistics as st, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cfg import load

C = load()
RC, RU, AL, WEB = C["race"], C.get("rules", {}), C.get("alerts", {}), C.get("web", {})
FOLLOW = {x.strip() for x in str(RC.get("follow") or "").split(",") if x.strip()}
DB = os.path.join(os.environ.get("DATA_DIR", "/data"), "race.sqlite")
PIT_LOSS = 156.6
_cache = [0, b"{}"]


def gap_key(g):
    g = (g or "").strip()
    if "uelta" in g:
        n = "".join(c for c in g if c.isdigit())
        return ("V", int(n)) if g[0].isdigit() else ("L", 0)
    try:
        r = 0
        for x in g.split(":"):
            r = r * 60 + float(x)
        return ("S", r)
    except ValueError:
        return (None, None)


def stints(L):
    S, cur = [], []
    for x in L:                       # x = (lap, ts, driver, ms, pit)
        if x[4] and cur:
            S.append(cur); cur = []
        cur.append(x)
    return S + ([cur] if cur else [])


def clean(s):
    return [x[3] / 1000 for x in s if not x[4] and x[3]]


def mdn(v):
    return st.median(v) if v else None


def build():
    now = time.time()
    con = sqlite3.connect(DB, timeout=5)
    meta = dict(con.execute("SELECT k,v FROM meta"))
    live = {r["kart"]: r for r in (json.loads(j) for (j,) in con.execute("SELECT j FROM live"))}
    laps = {}
    for k, l, ts, d, m, p in con.execute("SELECT kart,lap,ts,driver,ms,pit FROM lap ORDER BY kart,lap"):
        laps.setdefault(k, []).append((l, ts, d, m, p))
    pits = con.execute("SELECT kart,ts,kind,dur_s,laps FROM pit ORDER BY ts").fetchall()
    con.close()
    rem = max(0, meta["cd"] / 1000 - (now - meta["cd_ts"])) / 60 if meta.get("cd") else None
    allclean = [x[3] / 1000 for L in laps.values() for x in L[-30:] if not x[4] and x[3]]
    lref = mdn(allclean) or 85
    maxpits = max((r["pits"] for r in live.values()), default=0)
    minp = RU.get("min_pit_s")
    # --- cola de karts FIFO: el kart que deja la entrada i lo coge la entrada i+Q (Q = karts en la fila, desconocido)
    win = (AL.get("kart_window_min") or 15) * 60
    fd, sd = AL.get("kart_fast_delta_s", -.15), AL.get("kart_slow_delta_s", .2)
    E = []
    for k, ts, kind, dur, nl in pits:
        if kind != "in" or k not in laps: continue
        SS = stints(laps[k]); fin = next((x for x in SS if x[-1][0] == nl), None)
        c = clean(fin) if fin else []
        prev = [mdn(clean(x)) for x in SS if x is not fin and x[-1][0] < nl and len(clean(x)) >= 5][-5:]
        E.append(dict(kart=k, ts=ts, d=(mdn(c) - mdn(prev)) if len(c) >= 5 and prev else None, dur=sum(c)))
    E = E[-30:]
    fast = sum(1 for e in E if e["ts"] >= now - win and e["d"] is not None and e["d"] <= fd and e["dur"] >= 1200)
    slow = sum(1 for e in E if e["ts"] >= now - win and e["d"] is not None and e["d"] >= sd)
    kart_alerts = []
    if fast >= (AL.get("kart_fast_count") or 3):
        kart_alerts.append(("yellow", f"Cola de karts: {fast} equipos rápidos entraron en los últimos {win//60:.0f} min → probable kart BUENO si entras ahora"))
    if slow >= (AL.get("kart_slow_count") or 3):
        kart_alerts.append(("yellow", f"Cola de karts: {slow} equipos lentos entraron en los últimos {win//60:.0f} min → posible kart MALO"))

    def pool_advice(stint_left):
        """Probabilidad de kart bueno/malo según cuántos equipos entren antes que tú (Q uniforme en [min,max])."""
        n, qmax = len(E), AL.get("kart_queue_max") or 6
        Qs = range(AL.get("kart_queue_min") or 2, qmax + 1)
        if n < 3: return []
        gap = (E[-1]["ts"] - E[max(0, n - 10)]["ts"]) / max(1, min(9, n - 1))   # s entre entradas
        best, now_p = None, (0, 0)
        for kk in range(qmax + 1):                                              # kk = equipos que entran antes que tú
            wait = kk * gap
            if wait > (AL.get("kart_max_wait_min") or 8) * 60 or (stint_left is not None and wait > stint_left): break
            g = b = 0; names = []
            for Q in Qs:
                i = n + kk - Q
                e = E[i] if 0 <= i < n else None
                if e and e["d"] is not None:
                    g += e["d"] <= fd; b += e["d"] >= sd
                    if e["d"] <= fd: names.append(f"#{e['kart']} {e['d']:+.2f}s")
            sc = (g - b) / len(Qs)
            if kk == 0: now_p = (g / len(Qs), b / len(Qs))
            if best is None or sc > best[0] + 1e-9: best = (sc, kk, wait, g / len(Qs), sorted(set(names)))
        if not best: return []
        sc, kk, wait, pg, names = best; res = []
        if now_p[1] >= .5: res.append(("yellow", f"Si entras ya: {now_p[1]*100:.0f}% de kart LENTO según las últimas entradas a box"))
        if kk > 0 and sc > now_p[0] - now_p[1] + .2 and pg > 0:
            res.append(("yellow", f"Mejor momento para entrar: tras ~{kk} equipos más (≈{wait/60:.1f} min, {wait/lref:.1f} vueltas): {pg*100:.0f}% de kart bueno ({', '.join(names)}). Si no entra nadie entre medias te tocan karts anteriores"))
        elif kk == 0 and pg > 0: res.append(("info", f"Si entras ya: {pg*100:.0f}% de kart bueno ({', '.join(names)})"))
        return res
    # --- por equipo
    rows = []
    for k, r in live.items():
        L = laps.get(k, []); S = stints(L) if L else []
        cur = S[-1] if S else []
        cl = clean(cur); a = []
        last_ts = L[-1][1] if L else None
        base = cur[1:] if cur and cur[0][4] else cur
        stint_s = None if r["in_pit"] or not L else (sum(x[3] or 0 for x in base) / 1000 + (min(now - last_ts, 240) if last_ts else 0))
        o = str(r.get("otr") or "").rstrip(".").split(":")
        if not r["in_pit"] and len(o) == 2 and all(x.isdigit() for x in o) and (len(base) < 2 or stint_s is None):
            stint_s = int(o[0]) * 3600 + int(o[1]) * 60          # "On Track" del WSS (h:m) si aún no hay vueltas propias
        pit_s = now - r["pit_in_ts"] if r["in_pit"] and r.get("pit_in_ts") else None
        avg3 = mdn(cl[-3:]) or lref
        mx = (RU.get("max_stint_min") or 0) * 60
        if mx and stint_s is not None:
            left = mx - stint_s; pct = left / mx * 100
            lvl = "red" if pct <= (AL.get("stint_red_pct") or 6) else "yellow" if pct <= (AL.get("stint_yellow_pct") or 12) else None
            if lvl: a.append((lvl, f"Tiempo máximo en pista: quedan {max(left,0)/60:.1f} min (~{max(left,0)/avg3:.0f} vueltas a ritmo de las últimas 3: {avg3:.1f}s)"))
        # ritmo del stint vs stints previos (mismo piloto > resto del equipo)
        if len(S) > 1 and len(cl) >= 3 and not r["in_pit"]:
            drv = cur[-1][2]
            same = [mdn(clean(s)) for s in S[:-1] if s[-1][2] == drv and len(clean(s)) >= 5]
            other = [mdn(clean(s)) for s in S[:-1] if len(clean(s)) >= 5]
            ref, y, rd, who = (same, AL.get("driver_slower_yellow_s", .5), AL.get("driver_slower_red_s", 1.0), "sus stints anteriores") if same else (other, AL.get("team_slower_yellow_s", .8), AL.get("team_slower_red_s", 1.5), "otros pilotos del equipo")
            if ref:
                d = mdn(cl) - mdn(ref)
                if d >= rd: a.append(("red", f"Ritmo del stint {d:+.2f}s más lento que {who}"))
                elif d >= y: a.append(("yellow", f"Ritmo del stint {d:+.2f}s más lento que {who}"))
        if len(cl) >= 4 and not cur[-1][4] and cur[-1][3] and cur[-1][3] / 1000 - mdn(cl[-4:-1]) >= (AL.get("lap_slow_s") or 1.5):
            a.append(("yellow", f"Última vuelta {cur[-1][3]/1000 - mdn(cl[-4:-1]):+.1f}s más lenta que la media de las 3 anteriores"))
        if last_ts and not r["in_pit"] and now - last_ts > (AL.get("stopped_factor") or 2.5) * lref:
            a.append(("red", f"Sin pasar por meta hace {now-last_ts:.0f}s (¿parado en pista?)"))
        durs = [d for kk, ts, kind, d, _ in pits if kk == k and kind == "out" and d is not None]
        if minp and durs and durs[-1] < minp - 3:
            a.append(("yellow", f"Última parada corta: {durs[-1]:.0f}s (mínimo {minp}s)"))
        need = (RU.get("min_stops") or 0) - r["pits"]
        if need > 0 and rem is not None:
            pc = RU.get("pit_close_remaining_min") or 0
            lvl = "red" if rem <= pc + need * 5 else "yellow" if rem <= pc + need * 45 else None
            if lvl: a.append((lvl, f"Faltan {need} paradas obligatorias; quedan {rem:.0f} min (pit cierra a {pc} min)"))
        tot = {}
        for x in L: tot[x[2]] = tot.get(x[2], 0) + (x[3] or 0) / 1000
        for key, sign in (("min_driver_min", 1), ("max_driver_min", -1)):
            v = RU.get(key)
            if not v or not tot: continue
            if sign == 1 and rem is not None:
                miss = sum(max(0, v * 60 - t) for t in tot.values())
                if miss > 0 and miss * 1.15 > rem * 60: a.append(("red" if miss > rem * 60 else "yellow", f"Tiempo mínimo por piloto: faltan {miss/60:.0f} min entre pilotos y quedan {rem:.0f} min"))
            elif sign == -1:
                for d, t in tot.items():
                    if t >= v * 60 * .94: a.append(("red", f"{d}: {t/60:.0f}/{v} min (máximo por piloto)"))
                    elif t >= v * 60 * .88: a.append(("yellow", f"{d}: {t/60:.0f}/{v} min (máximo por piloto)"))
        if k in FOLLOW:
            a += kart_alerts
            if not r["in_pit"]: a += pool_advice(mx - stint_s if mx and stint_s is not None else None)
        lvl = "red" if any(x[0] == "red" for x in a) else "yellow" if any(x[0] == "yellow" for x in a) else "green" if L else "grey"
        score = r["laps"] - (r["race_s"] or 0) / lref - (maxpits - r["pits"]) * PIT_LOSS / lref
        rows.append(dict(r, level=lvl, alerts=[dict(level=x, text=y) for x, y in a], stint_s=stint_s, pit_s=pit_s, follow=k in FOLLOW, score=score))
    rows.sort(key=lambda x: x["pos"] or 999)
    for i, x in enumerate(sorted(rows, key=lambda x: -x["score"])): x["vpos"] = i + 1
    prev = None
    for x in rows:
        g, p = gap_key(x["gap"]), prev
        x["interval"] = ""
        if p and g[0] == "S" and p[0][0] in ("S", "L"): x["interval"] = f"{g[1] - (p[0][1] if p[0][0]=='S' else 0):.3f}"
        elif p and g[0] in ("S", "V") and p[0][0] == "V": x["interval"] = f"{(g[1] if g[0]=='V' else 0) - p[0][1]} V"
        prev = (g, x)
    return dict(name=RC.get("name") or meta.get("title"), title=meta.get("title"), track=meta.get("track"), remaining_s=None if rem is None else rem * 60, now=now, teams=rows, kart_alerts=[dict(level=a, text=b) for a, b in kart_alerts], rules=RU)


class H(BaseHTTPRequestHandler):
    def do_GET(s):
        if s.path.startswith("/api/state"):
            if time.time() - _cache[0] > 1:
                try: _cache[1], _cache[0] = json.dumps(build()).encode(), time.time()
                except Exception as e: _cache[1] = json.dumps({"error": str(e), "teams": []}).encode()
            body, ct = _cache[1], "application/json"
        else:
            body, ct = open(os.path.join(os.path.dirname(__file__), "index.html"), "rb").read(), "text/html; charset=utf-8"
        s.send_response(200); s.send_header("Content-Type", ct); s.end_headers(); s.wfile.write(body)

    def log_message(s, *a): pass


if __name__ == "__main__":
    port = int(WEB.get("port") or 8080)
    print(f"web en http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
