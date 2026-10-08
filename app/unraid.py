"""Read disk spin state from Unraid's emhttp disks.ini."""
import os
import re

SECTION_RE = re.compile(r'^\["?([^"\]]+)"?\]\s*$')
KV_RE = re.compile(r'^(\w+)\s*=\s*"?(.*?)"?\s*$')


def parse_disks_ini(text):
    disks, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        m = SECTION_RE.match(line)
        if m:
            cur = {"id": m.group(1)}
            disks.append(cur)
            continue
        m = KV_RE.match(line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
    out = []
    for d in disks:
        if d.get("type") == "Flash" or not d.get("device"):
            continue
        out.append({
            "name": d.get("name") or d["id"],
            "device": d.get("device"),
            "type": d.get("type", ""),
            "status": d.get("status", ""),
            "asleep": d.get("spundown") == "1",
            "temp": d.get("temp") if d.get("temp") not in (None, "", "*") else None,
            "rotational": d.get("rotational", "1") != "0",
        })
    return out


def read_disks(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_disks_ini(f.read())
    except OSError:
        return None
