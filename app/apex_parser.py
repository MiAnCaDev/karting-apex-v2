"""
Parser de los frames del WSS de Apex Timing.

Protocolo observado (frames de texto, lineas separadas por \n, campos por '|'):
  init|r|                       -> reinicio de estado
  title1||..., title2||..., track||..., wth2||...   -> metadatos
  dyn1|countdown|<ms>           -> cuenta atras de la carrera (ms)
  com||<html>                   -> comentarios de direccion de carrera
  grid||<tbody>...</tbody>      -> parrilla completa (HTML). Cabecera con data-type por columna
  r<ID>c<N>|<clase>|<valor>     -> actualizacion de una celda (equipo ID, columna N)
        clase 'drteam' = piloto actual "NOMBRE [h:mm]" ; 'dr' = nombre del equipo
        clase 'tn' normal, 'tb' mejor global, 'ib'/'in' texto, 'to' tiempo en pit en vivo ...
  r<ID>|*|<vuelta_ms>|<ms>      -> fin de vuelta (datos para tracking)
  r<ID>|*i1|<ms>, *i2|<ms>      -> paso por intermedios
  r<ID>|*in|0 / *out|0          -> entrada / salida de pit
  r<ID>|#|<pos>                 -> posicion

Uso:  python apex_parser.py frames.jsonl [laps.csv]
"""
import csv, json, re, sys
from dataclasses import dataclass, field

LINE_RE = re.compile(r"^(r\d+)(?:c(\d+))?\|(.*)$")


def parse_time(s):
    """'1:23.706' | '26.546' -> segundos (float) o None"""
    s = (s or "").strip()
    m = re.fullmatch(r"(?:(\d+):)?(\d+)\.(\d+)", s)
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2)) + float("0." + m.group(3))


def split_driver(txt):
    m = re.fullmatch(r"\s*(.*?)\s*\[(\d+:\d+)\]\s*", txt or "")
    return (m.group(1), m.group(2)) if m else ((txt or "").strip(), None)


@dataclass
class Team:
    rid: str
    cells: dict = field(default_factory=dict)  # col(int) -> (clase, valor)
    driver: str = ""
    driver_time: str = ""
    team_name: str = ""
    in_pit: bool = False

    def v(self, col):
        return self.cells.get(col, ("", ""))[1]


class ApexState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.meta, self.teams, self.cols, self.events = {}, {}, {}, []
        self.countdown_ms = None

    # ---- init grid -------------------------------------------------
    def load_grid(self, html):
        self.cols = {int(c): t for c, t in re.findall(r'data-id="c(\d+)" data-type="([^"]*)"', html)}
        self.teams = {}
        for rid, body in re.findall(r'<tr data-id="(r\d+)"[^>]*>(.*?)</tr>', html, re.S):
            if rid == "r0":
                continue
            t = Team(rid)
            for cid, cls, val in re.findall(r'data-id="r\d+c(\d+)" class="([^"]*)"[^>]*>(.*?)</(?:td|p|div)>', body):
                t.cells[int(cid)] = (cls, val)
            t.team_name = t.v(self.col("dr")).strip()
            t.driver, t.driver_time = split_driver("")
            self.teams[rid] = t

    def col(self, typ):
        for c, t in self.cols.items():
            if t == typ:
                return c
        return -1

    # ---- aplicar un frame (varias lineas) ---------------------------
    def apply_frame(self, text, ts):
        before = {rid: self._snapshot(t) for rid, t in self.teams.items()}
        pit_events = []
        for line in text.split("\n"):
            if not line:
                continue
            key, _, rest = line.partition("|")
            if key == "init":
                self.reset()
            elif key == "grid":
                self.load_grid(rest.split("|", 1)[1] if rest.startswith("|") else rest)
            elif key == "dyn1":
                _, _, v = rest.partition("|")
                self.countdown_ms = int(v) if v.isdigit() else None
            elif key in ("title1", "title2", "track", "wth2", "com", "msg"):
                self.meta[key] = rest.split("|", 1)[-1]
            else:
                m = LINE_RE.match(line)
                if m:
                    pit_events += self._cell_or_row(m, ts)
        evs = []
        for rid, t in self.teams.items():
            b = before.get(rid)
            if b is None:
                continue
            laps_c = self.col("tlp")
            if t.v(laps_c) != b["laps"] and t.v(laps_c).isdigit():
                evs.append(self._lap_event(t, ts))
            if t.driver and b["driver"] and t.driver != b["driver"]:
                evs.append({"type": "driver_change", "ts": ts, "rid": rid, "team": t.team_name, "kart": t.v(self.col("no")),
                            "from": b["driver"], "to": t.driver})
        evs += pit_events
        self.events += evs
        return evs

    def _snapshot(self, t):
        return {"laps": t.v(self.col("tlp")), "driver": t.driver}

    def _cell_or_row(self, m, ts):
        rid, col, payload = m.group(1), m.group(2), m.group(3)
        t = self.teams.get(rid)
        if t is None:
            return []
        out = []
        if col:
            cls, _, val = payload.partition("|")
            c = int(col)
            if cls == "drteam":
                t.driver, t.driver_time = split_driver(val)
            elif cls == "dr":
                t.team_name = val.strip() or t.team_name
            else:
                t.cells[c] = (cls, val)
        else:
            parts = payload.split("|")
            kind = parts[0]
            if kind in ("*in", "*out"):
                t.in_pit = kind == "*in"
                out.append({"type": "pit_in" if t.in_pit else "pit_out", "ts": ts, "rid": t.rid, "team": t.team_name,
                            "kart": t.v(self.col("no")), "driver": t.driver,
                            "laps": t.v(self.col("tlp")), "pits": t.v(self.col("pit"))})
        return out

    def _lap_event(self, t, ts):
        g = lambda typ: t.v(self.col(typ))
        return {
            "type": "lap", "ts": ts, "team": t.team_name, "rid": t.rid, "kart": g("no"),
            "driver": t.driver, "driver_total": t.driver_time,
            "pos": g("rk"), "lap": g("tlp"), "last_lap": g("llp"),
            "s1": g("s1"), "s2": g("s2"), "s3": g("s3"),
            "gap": g("gap"), "best": g("blp"), "pits": g("pit"),
            "pit_or_track_time": g("otr"), "race_time": t.v(16),
            "countdown_ms": self.countdown_ms,
        }


def replay(path, out_csv=None):
    st = ApexState()
    n = 0
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        for ev in st.apply_frame(r["d"], r["t"]):
            n += 1
            print(ev)
    print(f"\n{len(st.teams)} equipos, {n} eventos, meta={st.meta.get('title1')} / {st.meta.get('track')}")
    laps = [e for e in st.events if e["type"] == "lap"]
    if out_csv and laps:
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(laps[0].keys()))
            w.writeheader()
            w.writerows(laps)
    return st


if __name__ == "__main__":
    replay(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
