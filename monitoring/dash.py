#!/usr/bin/env python3
"""Terminal dashboard for the CPX200DP PSU, read from the Prometheus HTTP API.

No browser, stdlib only. Run on the Prometheus host (or anywhere that can reach
it, e.g. through an ssh -L 9090 forward):

    python3 dash.py                      # one-shot snapshot
    python3 dash.py --watch              # live, refresh every 2s
    python3 dash.py --watch 5            # live, every 5s
    python3 dash.py --url http://host:9090
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

BARS = "▁▂▃▄▅▆▇█"
JOB = 'job="cpx_psu"'


def color(s, *codes):
    return f"\033[{';'.join(codes)}m{s}\033[0m"


BOLD, DIM, RED, GREEN, YELLOW, CYAN = "1", "2", "31", "32", "33", "36"


def _get(url, path, params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}{path}?{q}", timeout=10) as r:
        body = json.load(r)
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "query failed"))
    return body["data"]["result"]


def instant(url, expr):
    """Return {channel_or_None: float} for a vector result."""
    out = {}
    for s in _get(url, "/api/v1/query", {"query": expr}):
        out[s["metric"].get("channel")] = float(s["value"][1])
    return out


def series(url, expr, minutes, step):
    """Return {channel_or_None: [float, ...]} over the range."""
    end = time.time()
    out = {}
    for s in _get(url, "/api/v1/query_range", {
        "query": expr, "start": end - minutes * 60, "end": end, "step": step,
    }):
        out[s["metric"].get("channel")] = [float(v) for _, v in s["values"]]
    return out


def idn(url):
    for s in _get(url, "/api/v1/query", {"query": "cpx_info"}):
        return s["metric"].get("idn", "?")
    return None


def alerts(url):
    out = []
    for s in _get(url, "/api/v1/query", {"query": 'ALERTS{alertstate="firing"}'}):
        m = s["metric"]
        out.append((m.get("alertname", "?"), m.get("severity", ""), m.get("channel", "")))
    return out


def spark(vals):
    if not vals:
        return color("(no data)", DIM)
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    return "".join(BARS[min(7, int((v - lo) / rng * 7))] for v in vals)


def fmt(d, ch, unit="", nd=3):
    v = d.get(ch)
    return f"{v:.{nd}f}{unit}" if v is not None else color("-", DIM)


def render(url, minutes, step):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = []

    # --- health -----------------------------------------------------------
    try:
        up = instant(url, f"up{{{JOB}}}").get(None, 0)
        cpx_up = instant(url, "cpx_up").get(None, 0)
    except Exception as e:
        return color(f"cannot reach Prometheus at {url}: {e}", RED)
    scrape = color(" SCRAPE UP ", BOLD, GREEN) if up else color(" SCRAPE DOWN ", BOLD, RED)
    link = color(" PSU OK ", BOLD, GREEN) if cpx_up else color(" PSU UNREACHABLE ", BOLD, RED)
    paused = instant(url, "cpx_polling_paused").get(None, 0)
    pflag = color("  polling PAUSED", YELLOW) if paused else ""

    L.append(color("┌─ CPX200DP power supply ", BOLD) + color(f"· {now}", DIM))
    L.append(f"│ {scrape}  {link}{pflag}")
    if up:
        info = idn(url) or "?"
        L.append("│ " + color(info, CYAN))
        mv = instant(url, "cpx_max_voltage_volts").get(None)
        mc = instant(url, "cpx_max_current_amps").get(None)
        if mv is not None and mc is not None:
            L.append("│ " + color(f"ceilings: {mv:.1f} V / {mc:.2f} A", DIM))

    # --- per channel ------------------------------------------------------
    if up:
        setv = instant(url, "cpx_set_voltage_volts")
        seti = instant(url, "cpx_set_current_amps")
        outv = instant(url, "cpx_output_voltage_volts")
        outi = instant(url, "cpx_output_current_amps")
        power = instant(url, "cpx_output_power_watts")
        enabled = instant(url, "cpx_output_enabled")
        cv = instant(url, "cpx_constant_voltage")
        cc = instant(url, "cpx_constant_current")
        tov = instant(url, "cpx_trip_overvoltage")
        toc = instant(url, "cpx_trip_overcurrent")
        sv = series(url, "cpx_output_voltage_volts", minutes, step)
        si = series(url, "cpx_output_current_amps", minutes, step)

        for ch in sorted(k for k in outv if k is not None):
            on = enabled.get(ch, 0)
            state = color(" ON ", BOLD, GREEN) if on else color(" OFF ", BOLD, DIM)
            mode = "CV" if cv.get(ch) else "CC" if cc.get(ch) else "-"
            mode_c = color(mode, GREEN if mode == "CV" else YELLOW if mode == "CC" else DIM)
            trips = []
            if tov.get(ch):
                trips.append(color("OVP", BOLD, RED))
            if toc.get(ch):
                trips.append(color("OCP", BOLD, RED))
            trip_s = "  trip:" + " ".join(trips) if trips else ""

            L.append("│")
            L.append(f"│ {color(f'Channel {ch}', BOLD)}  {state}  mode {mode_c}{trip_s}")
            L.append(f"│   set    {fmt(setv, ch, ' V'):>12}   {fmt(seti, ch, ' A'):>12}")
            L.append(f"│   output {color(fmt(outv, ch, ' V'), BOLD):>12}   "
                     f"{color(fmt(outi, ch, ' A'), BOLD):>12}   "
                     f"{color(fmt(power, ch, ' W', 2), CYAN)}")
            L.append(f"│   V {spark(sv.get(ch, []))}")
            L.append(f"│   I {spark(si.get(ch, []))}")

    # --- alerts -----------------------------------------------------------
    try:
        firing = alerts(url)
    except Exception:
        firing = []
    L.append("│")
    if firing:
        L.append("│ " + color("FIRING ALERTS:", BOLD, RED))
        for name, sev, ch in firing:
            tag = f" [{sev}]" if sev else ""
            chs = f" ch{ch}" if ch else ""
            L.append("│   " + color(f"● {name}{chs}{tag}", RED))
    else:
        L.append("│ " + color("no alerts firing", GREEN))
    L.append(color(f"└─ prometheus {url} · last {minutes} min", DIM))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Terminal dashboard for the CPX200DP via Prometheus.")
    ap.add_argument("--url", default="http://localhost:9090", help="Prometheus base URL")
    ap.add_argument("--watch", nargs="?", const=2, type=float, default=None,
                    metavar="SEC", help="refresh continuously every SEC seconds (default 2)")
    ap.add_argument("--window", type=int, default=15, help="sparkline window in minutes")
    ap.add_argument("--step", type=int, default=15, help="sparkline sample step in seconds")
    args = ap.parse_args()

    if args.watch is None:
        print(render(args.url, args.window, args.step))
        return
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")  # clear + home
            sys.stdout.write(render(args.url, args.window, args.step) + "\n")
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
