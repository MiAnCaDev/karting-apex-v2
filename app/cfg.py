import configparser, os


def conv(v):
    v = v.strip()
    if v == "":
        return None
    if v.lower() in ("false", "no", "off"):
        return False
    if v.lower() in ("true", "yes", "on"):
        return True
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v


def load(path=None):
    p = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    p.read(path or os.environ.get("RACE_CFG", "/app/race.cfg"), encoding="utf-8")
    return {s: {k: conv(v) for k, v in p[s].items()} for s in p.sections()}
