"""
Cliente del endpoint de historico de Apex Timing (request.php).

POST https://live-data.apex-timing.com/live-timing/commonv2/functions/request.php
     port=<puerto>&request=D#<n>#D<team>.<tipo>#<n>#D<team>.<tipo>...

  <n> negativo = "ultimos |n|" (-30 = ultimas 30 vueltas, -999 = todas); positivo = cuantos registros.
  tipos: L = vueltas, P = paradas/stints, B = mejores (BL, BS), INF = pilotos del equipo (XML)

Respuesta (texto, una linea por registro:  D<team>.<clave>#<valor>):
  D1191.L1181#s1|s2|s3|vuelta           (ms; prefijo 'r' en un valor = marca especial, ver abajo)
  D1191.P35#n|vuelta|t_in|t_out|pit_ms|stint_ms|stint_vueltas|driver_id|driver_acum_ms
  D1191.BL#s1|s2|s3|vuelta              (mejor vuelta)
  D1191.BS#a|b|c                        (SIN CONFIRMAR: mejores sectores?)
  D1191.INF#<driver id= num= name= ...><driver .../>...</driver>

Uso:
  python apex_history.py --port 8800 --teams 1191,1186 --db carrera.sqlite
  python apex_history.py --port 8800 --frames frames.jsonl --db carrera.sqlite   # IDs de equipo desde el WSS
  python apex_history.py --from-file respuesta.txt                               # prueba offline
"""
import argparse, json, re, sqlite3, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

URL = "https://live-data.apex-timing.com/live-timing/commonv2/functions/request.php"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://live.apex-timing.com",
    "Referer": "https://live.apex-timing.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/154.0 Safari/537.36",
    "Accept": "text/html, */*; q=0.01",
}


def build_request(team, laps=-999, pits=-999, best=2, inf=1):
    return f"D#{laps}#D{team}.L#{pits}#D{team}.P#{best}#D{team}.B#{inf}#D{team}.INF"


def fetch(port, request, timeout=20):
    data = urllib.parse.urlencode({"port": port, "request": request}).encode()
    req = urllib.request.Request(URL, data=data, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- parseo
KEY_RE = re.compile(r"^D(\d+)\.(L(\d+)|P(\d+)|BL|BS|INF)$")


def _num(s):
    """'r22240' -> (22240, True) ; '26758' -> (26758, False)"""
    s = s.strip()
    flag = s.startswith("r")
    s = s[1:] if flag else s
    return (int(s) if s.lstrip("-").isdigit() else None), flag


def _int(s):
    s = s.strip()
    return int(s) if s.lstrip("-").isdigit() else None


def parse_response(text):
    teams = defaultdict(lambda: {"laps": {}, "pits": {}, "best_lap": None, "bs": None, "drivers": {}, "info": {}})
    for line in text.splitlines():
        key, sep, val = line.partition("#")
        m = KEY_RE.match(key.strip())
        if not sep or not m:
            continue
        tid, kind = int(m.group(1)), m.group(2)
        t = teams[tid]
        try:
            _parse_line(t, kind, m, val)
        except Exception as e:  # una linea rara no debe tumbar el volcado
            t.setdefault("errors", []).append((line[:120], repr(e)))
    return dict(teams)


def _parse_line(t, kind, m, val):
    if True:
        if kind.startswith("L"):
            vals = [_num(x) for x in val.split("|")]
            vals += [(None, False)] * (4 - len(vals))
            t["laps"][int(m.group(3))] = {
                "s1": vals[0][0], "s2": vals[1][0], "s3": vals[2][0], "time": vals[3][0],
                "flag_r": any(f for _, f in vals),
            }
        elif kind.startswith("P"):
            f = [_int(x) for x in val.split("|")]
            f += [None] * (9 - len(f))
            t["pits"][int(m.group(4))] = dict(n=f[0], lap=f[1], t_in=f[2], t_out=f[3], pit_ms=f[4],
                                   stint_ms=f[5], stint_laps=f[6], driver_id=f[7], driver_total_ms=f[8])
        elif kind == "BL":
            v = [_int(x) for x in val.split("|")]
            v += [None] * (4 - len(v))
            t["best_lap"] = dict(s1=v[0], s2=v[1], s3=v[2], time=v[3])
        elif kind == "BS":
            t["bs"] = [_int(x) for x in val.split("|")]
        elif kind == "INF":
            root = ET.fromstring(val)
            t["info"] = {"kart": root.get("num"), "name": root.get("name", "").strip(), "nat": root.get("nat")}
            for d in root.findall("driver"):
                t["drivers"][int(d.get("id"))] = {
                    "num": int(d.get("num")), "name": d.get("name").strip(),
                    "current": d.get("current") == "1"}
    return None


def enrich(t):
    """Asigna piloto a cada vuelta y marca vueltas de pit.
    Stint k = vueltas (pit[k-1].lap+1 .. pit[k].lap], piloto = pit[k].driver_id.
    La vuelta pit[k].lap+1 es la que contiene la parada k (s1 largo)."""
    pits = [t["pits"][k] for k in sorted(t["pits"]) if t["pits"][k]["lap"] is not None]
    pit_laps = {p["lap"] + 1 for p in pits}
    current = next((i for i, d in t["drivers"].items() if d["current"]), None)
    bounds = [(p["lap"], p["driver_id"]) for p in pits]
    for n, lap in t["laps"].items():
        drv = current
        for last_lap, did in bounds:
            if n <= last_lap:
                drv = did
                break
        lap["driver_id"] = drv
        lap["pit_lap"] = n in pit_laps
    return t


# ---------------------------------------------------------------- resumen y BBDD
def ms(x):
    if x is None:
        return "-"
    m, s = divmod(x / 1000, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:06.3f}" if h else f"{m}:{s:06.3f}"


def summary(tid, t):
    print(f"\n=== {t['info'].get('name')} (equipo {tid}, kart {t['info'].get('kart')}) ===")
    print(f"vueltas cargadas: {len(t['laps'])}  paradas: {len(t['pits'])}  lineas con error: {len(t.get('errors', []))}")
    per = defaultdict(list)
    for lap in t["laps"].values():
        if not lap["pit_lap"] and lap["time"] is not None:
            per[lap["driver_id"]].append(lap["time"])
    tot = {t["pits"][k]["driver_id"]: t["pits"][k]["driver_total_ms"] for k in sorted(t["pits"])}  # ultimo valor por piloto
    for did, d in t["drivers"].items():
        v = per.get(did, [])
        avg = ms(sum(v) / len(v)) if v else "-"
        print(f"  piloto {d['num']} {d['name']:<32} vueltas limpias={len(v):>4}  media={avg}  acum(a ultima parada)={ms(tot[did]) if did in tot else '-'}")
    if t["pits"]:
        pm = [p["pit_ms"] for p in t["pits"].values() if p["pit_ms"] is not None]
        if pm: print(f"  pit: min={ms(min(pm))} media={ms(sum(pm)/len(pm))} max={ms(max(pm))}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS team(id INTEGER PRIMARY KEY, kart TEXT, name TEXT, nat TEXT);
CREATE TABLE IF NOT EXISTS driver(id INTEGER PRIMARY KEY, team_id INT, num INT, name TEXT);
CREATE TABLE IF NOT EXISTS lap(team_id INT, lap INT, s1 INT, s2 INT, s3 INT, time INT,
  flag_r INT, pit_lap INT, driver_id INT, PRIMARY KEY(team_id, lap));
CREATE TABLE IF NOT EXISTS pit(team_id INT, n INT, lap INT, t_in INT, t_out INT, pit_ms INT,
  stint_ms INT, stint_laps INT, driver_id INT, driver_total_ms INT, PRIMARY KEY(team_id, n));
"""


def save(db, teams):
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    for tid, t in teams.items():
        con.execute("INSERT OR REPLACE INTO team VALUES(?,?,?,?)", (tid, t["info"].get("kart"), t["info"].get("name"), t["info"].get("nat")))
        for did, d in t["drivers"].items():
            con.execute("INSERT OR REPLACE INTO driver VALUES(?,?,?,?)", (did, tid, d["num"], d["name"]))
        for n, l in t["laps"].items():
            con.execute("INSERT OR REPLACE INTO lap VALUES(?,?,?,?,?,?,?,?,?)",
                        (tid, n, l["s1"], l["s2"], l["s3"], l["time"], int(l["flag_r"]), int(l["pit_lap"]), l["driver_id"]))
        for p in t["pits"].values():
            con.execute("INSERT OR REPLACE INTO pit VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (tid, p["n"], p["lap"], p["t_in"], p["t_out"], p["pit_ms"], p["stint_ms"], p["stint_laps"], p["driver_id"], p["driver_total_ms"]))
    con.commit()
    con.close()


def team_ids_from_frames(path):
    sys.path.insert(0, ".")
    from apex_parser import ApexState
    st = ApexState()
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        st.apply_frame(r["d"], r["t"])
        if st.teams:
            break
    return [int(rid[1:]) for rid in st.teams]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="8800")
    ap.add_argument("--teams", help="IDs separados por coma (ej. 1191,1186)")
    ap.add_argument("--frames", help="frames.jsonl del WSS para sacar todos los IDs")
    ap.add_argument("--laps", type=int, default=-999, help="-999 = todas, -30 = ultimas 30")
    ap.add_argument("--db")
    ap.add_argument("--from-file", help="respuesta guardada (modo offline)")
    ap.add_argument("--from-dir", help="carpeta dump/<fecha> creada por apex_dump.py")
    ap.add_argument("--delay", type=float, default=0.5, help="segundos entre peticiones")
    a = ap.parse_args()

    if a.from_dir:
        import glob
        txt = "\n".join(open(p, encoding="utf-8").read() for p in sorted(glob.glob(a.from_dir + "/D*.txt")))
        teams = parse_response(txt)
    elif a.from_file:
        teams = parse_response(open(a.from_file, encoding="utf-8").read())
    else:
        ids = [int(x) for x in a.teams.split(",")] if a.teams else team_ids_from_frames(a.frames)
        teams = {}
        for tid in ids:
            txt = fetch(a.port, build_request(tid, laps=a.laps))
            teams.update(parse_response(txt))
            print(f"equipo {tid}: ok", file=sys.stderr)
            time.sleep(a.delay)
    for tid, t in teams.items():
        enrich(t)
        summary(tid, t)
    if a.db:
        save(a.db, teams)
        print(f"\nguardado en {a.db}")


if __name__ == "__main__":
    main()
