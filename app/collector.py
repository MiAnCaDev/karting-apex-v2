"""Servicio colector: WSS -> SQLite (vuelta a vuelta, pits, estado en vivo). Backfill por request.php.
   python collector.py [--replay frames.jsonl]  (replay = prueba offline)"""
import asyncio, json, os, sqlite3, statistics as st, sys, time
from collections import deque
from apex_parser import ApexState, parse_time
from cfg import load

DATA = os.environ.get("DATA_DIR", "/data")
SCHEMA = """CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v);
CREATE TABLE IF NOT EXISTS lap(kart TEXT,lap INT,ts REAL,driver TEXT,ms INT,s1 INT,s2 INT,s3 INT,race_s REAL,pos INT,pit INT,PRIMARY KEY(kart,lap));
CREATE TABLE IF NOT EXISTS pit(kart TEXT,ts REAL,kind TEXT,driver TEXT,laps INT,dur_s REAL);
CREATE TABLE IF NOT EXISTS live(kart TEXT PRIMARY KEY, j TEXT);"""


def secs(s):
    try:
        r = 0
        for x in str(s).split(":"):
            r = r * 60 + float(x)
        return r
    except ValueError:
        return None


def ms(s):
    v = parse_time(s)
    return None if v is None else round(v * 1000)


class Collector:
    def __init__(self, C):
        self.C, self.S, self.field, self.pit_in = C, ApexState(), deque(maxlen=400), {}
        os.makedirs(DATA, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(DATA, "race.sqlite"), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        R = C["race"]
        key = f"{R.get('wss_url')}|{R.get('race_url')}"
        old = self.db.execute("SELECT v FROM meta WHERE k='race_key'").fetchone()
        mode = (C.get("storage", {}).get("reset_db") or "new_race")
        if mode == "always" or (mode == "new_race" and old and old[0] != key):
            for t in ("lap", "pit", "live", "meta"):
                self.db.execute(f"DELETE FROM {t}")
            print("BD reiniciada")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('race_key',?)", (key,))
        self.db.commit()

    def ingest(self, text, ts):
        S, db = self.S, self.db
        evs = S.apply_frame(text, ts)
        for ev in evs:
            k = ev["kart"]
            if ev["type"] == "lap":
                v = ms(ev["last_lap"])
                med = st.median(self.field) if self.field else None
                pit = int(any(t.in_pit for t in S.teams.values() if t.v(S.col("no")) == k) or bool(v and med and v > 1.25 * med))
                if v and not pit:
                    self.field.append(v)
                db.execute("INSERT OR REPLACE INTO lap VALUES(?,?,?,?,?,?,?,?,?,?,?)", (k, int(ev["lap"]), ts, ev["driver"], v, ms(ev["s1"]), ms(ev["s2"]), ms(ev["s3"]), secs(ev["race_time"]), int(ev["pos"] or 0), pit))
            elif ev["type"] in ("pit_in", "pit_out"):
                kind = ev["type"][4:]
                dur = ts - self.pit_in[k] if kind == "out" and k in self.pit_in else None
                if kind == "in":
                    self.pit_in[k] = ts
                db.execute("INSERT INTO pit VALUES(?,?,?,?,?,?)", (k, ts, kind, ev["driver"], int(ev["laps"] or 0), dur))
        if S.teams:
            c = S.col
            for t in S.teams.values():
                k = t.v(c("no"))
                row = dict(kart=k, name=t.team_name, driver=t.driver, pos=int(t.v(c("rk")) or 0) if t.v(c("rk")).isdigit() else 0,
                           laps=int(t.v(c("tlp")) or 0) if t.v(c("tlp")).isdigit() else 0, gap=t.v(c("gap")), last=t.v(c("llp")),
                           best=t.v(c("blp")), pits=int(t.v(c("pit")) or 0) if t.v(c("pit")).isdigit() else 0,
                           in_pit=t.in_pit, pit_in_ts=self.pit_in.get(k) if t.in_pit else None, race_s=secs(t.v(16)), otr=t.v(c("otr")).strip())
                db.execute("INSERT OR REPLACE INTO live VALUES(?,?)", (k, json.dumps(row)))
            for key, val in (("title", S.meta.get("title1")), ("track", S.meta.get("track")), ("cd", S.countdown_ms), ("cd_ts", ts)):
                if val is not None and (key != "cd_ts" or S.countdown_ms is not None):
                    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, val))
        db.commit()

    def backfill(self):
        """Recupera vueltas antiguas (request.php) de los equipos con huecos."""
        from apex_history import build_request, fetch, parse_response, enrich
        port = self.C["race"].get("history_port")
        if not port:
            return
        for rid, t in list(self.S.teams.items()):
            k, tid = t.v(self.S.col("no")), int(rid[1:])
            have = self.db.execute("SELECT COUNT(*) FROM lap WHERE kart=?", (k,)).fetchone()[0]
            if t.v(self.S.col("tlp")).isdigit() and have >= int(t.v(self.S.col("tlp"))) - 2:
                continue
            try:
                h = enrich(parse_response(fetch(port, build_request(tid, laps=-5000)))[tid])
                for n, l in h["laps"].items():
                    d = h["drivers"].get(l["driver_id"], {}).get("name", "")
                    self.db.execute("INSERT OR IGNORE INTO lap VALUES(?,?,?,?,?,?,?,?,?,?,?)", (k, n, None, d, l["time"], l["s1"], l["s2"], l["s3"], None, 0, int(l["pit_lap"])))
                self.db.commit()
                print(f"backfill #{k}: {len(h['laps'])} vueltas")
            except Exception as e:
                print("backfill fallo", k, e)
            time.sleep(0.3)


async def live(col, url):
    import websockets
    done = False
    while True:
        try:
            async with websockets.connect(url, origin="https://live.apex-timing.com", max_size=None, ping_interval=20) as ws:
                print("conectado", url)
                async for msg in ws:
                    col.ingest(msg, time.time())
                    if not done and col.S.teams:
                        done = True
                        asyncio.get_running_loop().run_in_executor(None, col.backfill)
        except Exception as e:
            print("desconectado:", e, "- reintento en 3s")
            await asyncio.sleep(3)


if __name__ == "__main__":
    C = load()
    col = Collector(C)
    if "--replay" in sys.argv:
        for line in open(sys.argv[sys.argv.index("--replay") + 1], encoding="utf-8"):
            r = json.loads(line)
            col.ingest(r["d"], r["t"])
        print("replay ok")
    else:
        asyncio.run(live(col, C["race"]["wss_url"]))
