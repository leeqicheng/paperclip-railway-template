#!/usr/bin/env python3
"""SubRouter spend report for an agent: read-only, prints to stdout, no server.

    export SUBROUTER_ACCESS_TOKEN="..."   # system access token from Personal Settings (not an sk- key)
    export SUBROUTER_USER_ID="..."        # numeric user id
    python3 subrouter_costs.py                 # month to date (UTC), JSON
    python3 subrouter_costs.py --days 7        # last 7 days
    python3 subrouter_costs.py --format md     # markdown tables instead of JSON

It only sends GET requests. It never creates, changes or deletes keys or subscriptions,
and it never prints the token or any sk- key value.
Exit codes: 0 ok, 2 missing configuration, 1 SubRouter error.
Standard library only, Python 3.8+.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("SUBROUTER_BASE_URL", "https://subrouter.ai").rstrip("/")
TOKEN = os.environ.get("SUBROUTER_ACCESS_TOKEN", "").strip()
USER_ID = os.environ.get("SUBROUTER_USER_ID", "").strip()
MAX_DAYS = 30          # the usage endpoint refuses longer ranges
PAGE_SIZE = 100


class ApiError(Exception):
    pass


def get(path, params=None, auth=True):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "subrouter-costs/1.0"}
    if auth:
        headers["Authorization"] = "Bearer " + TOKEN
        headers["New-Api-User"] = USER_ID
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ApiError("%s returned HTTP %s. %s" % (path, e.code, e.read().decode("utf-8", "replace")[:200]))
    except urllib.error.URLError as e:
        raise ApiError("Could not reach %s: %s" % (BASE, e.reason))
    try:
        payload = json.loads(text)
    except ValueError:
        raise ApiError("%s did not return JSON." % path)
    # Some business errors come back as HTTP 200, so check the body too.
    if isinstance(payload, dict) and (payload.get("success") is False or payload.get("message") == "error"):
        raise ApiError("%s failed: %s" % (path, payload.get("message") or payload.get("data") or "unknown error"))
    return payload


def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def items_of(payload):
    d = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(d, dict):
        for k in ("items", "list", "data"):
            if isinstance(d.get(k), list):
                return d[k]
        return []
    return d if isinstance(d, list) else []


def window(days):
    """Return (start, end, label). days=None means the current calendar month in UTC."""
    end = int(time.time())
    floor = end - MAX_DAYS * 86400 + 60
    if days is None:
        now = dt.datetime.now(dt.timezone.utc)
        start = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        return max(start, floor), end, "month to date (UTC)"
    days = max(1, min(MAX_DAYS, days))
    return max(end - days * 86400 + 60, floor), end, "last %d days" % days


def spend_by_model(start, end, qpu):
    by_model = {}
    for r in items_of(get("/api/data/self", {"start_timestamp": start, "end_timestamp": end})):
        if not isinstance(r, dict):
            continue
        name = r.get("model_name") or "(unknown)"
        a = by_model.setdefault(name, {"model": name, "usd": 0.0, "requests": 0, "tokens": 0})
        a["usd"] += (num(r.get("quota")) or 0) / qpu
        a["requests"] += int(num(r.get("count")) or 0)
        a["tokens"] += int(num(r.get("token_used")) or 0)
    rows = sorted(by_model.values(), key=lambda a: -a["usd"])
    for a in rows:
        a["usd"] = round(a["usd"], 4)
    return rows


def keys(qpu):
    """Lifetime spend per API key. Key values are deliberately left out."""
    out, seen = [], set()
    for page in range(1, 11):
        items = items_of(get("/api/token/", {"p": page, "page": page, "size": PAGE_SIZE, "page_size": PAGE_SIZE}))
        fresh = [t for t in items if isinstance(t, dict) and t.get("id") not in seen]
        for t in fresh:
            seen.add(t.get("id"))
            out.append({
                "name": t.get("name") or "",
                "status": "enabled" if t.get("status") == 1 else "disabled",
                "used_usd_lifetime": round((num(t.get("used_quota")) or 0) / qpu, 4),
                "remaining_usd": None if t.get("unlimited_quota") else round((num(t.get("remain_quota")) or 0) / qpu, 4),
            })
        if len(items) < PAGE_SIZE or not fresh:
            break
    return sorted(out, key=lambda k: -k["used_usd_lifetime"])


def report(days):
    status = get("/api/status", auth=False).get("data") or {}
    qpu = num(status.get("quota_per_unit")) or 500000.0
    start, end, label = window(days)
    out = {
        "source": BASE,
        "period": label,
        "start_utc": dt.datetime.fromtimestamp(start, dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "end_utc": dt.datetime.fromtimestamp(end, dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "currency": "USD",
    }
    out["models"] = spend_by_model(start, end, qpu)
    out["total_usd"] = round(sum(a["usd"] for a in out["models"]), 4)
    out["requests"] = sum(a["requests"] for a in out["models"])
    out["tokens"] = sum(a["tokens"] for a in out["models"])
    warnings = []
    try:
        me = get("/api/user/self").get("data") or {}
        out["account"] = {
            "balance_usd": round((num(me.get("quota")) or 0) / qpu, 4),
            "used_usd_lifetime": round((num(me.get("used_quota")) or 0) / qpu, 4),
        }
    except ApiError as e:
        warnings.append("Account balance unavailable: %s" % e)
    try:
        out["keys"] = keys(qpu)
    except ApiError as e:
        warnings.append("Per-key spend unavailable: %s" % e)
    if warnings:
        out["warnings"] = warnings
    return out


def markdown(r):
    lines = ["# SubRouter spend, %s" % r["period"], "",
             "%s to %s UTC" % (r["start_utc"], r["end_utc"]), "",
             "**Total: $%.2f** across %s requests and %s tokens." % (r["total_usd"], format(r["requests"], ","), format(r["tokens"], ","))]
    acct = r.get("account")
    if acct:
        lines.append("Balance left: $%.2f. Lifetime spend: $%.2f." % (acct["balance_usd"], acct["used_usd_lifetime"]))
    lines += ["", "| Model | USD | Share | Requests | Tokens |", "|---|---:|---:|---:|---:|"]
    total = r["total_usd"] or 0
    for a in r["models"]:
        share = "%.0f%%" % (100 * a["usd"] / total) if total > 0 else "–"
        lines.append("| %s | %.4f | %s | %s | %s |" % (a["model"], a["usd"], share, format(a["requests"], ","), format(a["tokens"], ",")))
    if not r["models"]:
        lines.append("| (no usage in this period) | 0 | – | 0 | 0 |")
    if r.get("keys"):
        lines += ["", "| API key | Status | Lifetime USD | Remaining USD |", "|---|---|---:|---:|"]
        for k in r["keys"]:
            left = "no limit" if k["remaining_usd"] is None else "%.2f" % k["remaining_usd"]
            lines.append("| %s | %s | %.4f | %s |" % (k["name"], k["status"], k["used_usd_lifetime"], left))
    for w in r.get("warnings", []):
        lines += ["", "Warning: %s" % w]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Read-only SubRouter spend report.")
    ap.add_argument("--days", type=int, default=None, help="look back this many days (1-30); default is month to date")
    ap.add_argument("--format", choices=("json", "md"), default="json")
    args = ap.parse_args()
    if not (TOKEN and USER_ID):
        print("Set SUBROUTER_ACCESS_TOKEN and SUBROUTER_USER_ID first.", file=sys.stderr)
        return 2
    try:
        r = report(args.days)
    except ApiError as e:
        print("SubRouter error: %s" % e, file=sys.stderr)
        return 1
    print(markdown(r) if args.format == "md" else json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
