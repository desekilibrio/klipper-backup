#!/usr/bin/env python3
import argparse, json, re, statistics, sys, time
import requests

BASE = "http://127.0.0.1:7125"
REQUEST_TIMEOUT = 60
LONG_GCODE_TIMEOUT = 600


def api_get(path, timeout=REQUEST_TIMEOUT):
    r = requests.get(BASE + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def api_post(path, payload=None, timeout=REQUEST_TIMEOUT):
    r = requests.post(BASE + path, json=payload or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def post_gcode(script, timeout=REQUEST_TIMEOUT):
    return api_post("/printer/gcode/script", {"script": script}, timeout=timeout)


def get_temps():
    data = api_get("/printer/objects/query?extruder&heater_bed")
    st = data["result"]["status"]
    return {
        "bed_temp": float(st["heater_bed"]["temperature"]),
        "bed_target": float(st["heater_bed"].get("target", 0.0)),
        "ext_temp": float(st["extruder"]["temperature"]),
        "ext_target": float(st["extruder"].get("target", 0.0)),
    }


def wait_for_bed(bed_target, tol=0.8, stable_s=15, timeout=1800):
    start = time.time()
    reached_at = None
    last = None
    while time.time() - start < timeout:
        t = get_temps()
        last = t
        if t["bed_temp"] >= (bed_target - tol):
            if reached_at is None:
                reached_at = time.time()
            elif time.time() - reached_at >= stable_s:
                return t
        else:
            reached_at = None
        time.sleep(2)
    raise RuntimeError(f"Timeout esperando cama estable. Bed={last['bed_temp']}/{bed_target}")


def wait_for_hotend(hotend_target, tol=0.8, stable_s=15, timeout=1800):
    start = time.time()
    reached_at = None
    last = None
    while time.time() - start < timeout:
        t = get_temps()
        last = t
        if t["ext_temp"] >= (hotend_target - tol):
            if reached_at is None:
                reached_at = time.time()
            elif time.time() - reached_at >= stable_s:
                return t
        else:
            reached_at = None
        time.sleep(2)
    raise RuntimeError(f"Timeout esperando hotend estable. Hotend={last['ext_temp']}/{hotend_target}")


def soak_bed(seconds, bed_target):
    if seconds <= 0:
        return
    end = time.time() + seconds
    while True:
        remain = int(end - time.time())
        if remain <= 0:
            break
        post_gcode(f"M140 S{bed_target}")
        if remain <= 10 or remain % 60 == 0:
            post_gcode(f"M117 Bed soak {remain}s")
        time.sleep(1)


def send_and_capture_value(cmd):
    post_gcode(cmd, timeout=LONG_GCODE_TIMEOUT)
    time.sleep(1.0)
    data = api_get("/server/gcode_store?count=200")
    msgs = data.get("result", {}).get("gcode_store", [])
    patt = re.compile(r"z_offset\s*[:=]\s*(-?\d+(?:\.\d+)?)", re.I)
    for item in reversed(msgs):
        msg = item.get("message", "")
        m = patt.search(msg)
        if m:
            return float(m.group(1)), msg
    raise RuntimeError("No pude capturar z_offset desde gcode_store")


def run_measurement_set(runs, material=None, pause_between=2.0):
    values = []
    raw = []
    cmd = "PRTOUCH_PROBE_ZOFFSET"
    if material:
        cmd += f" MATERIAL={material.upper()}"
    for _ in range(runs):
        val, txt = send_and_capture_value(cmd)
        values.append(val)
        raw.append(txt)
        time.sleep(pause_between)
    return values, raw


def summarize_all(values):
    vals = list(values)
    return {
        "all_values": vals,
        "all_mean": round(statistics.mean(vals), 6),
        "all_min": round(min(vals), 6),
        "all_max": round(max(vals), 6),
        "all_range": round(max(vals) - min(vals), 6),
        "all_std": round(statistics.pstdev(vals), 6) if len(vals) > 1 else 0.0,
    }


def summarize_trimmed(values):
    vals = sorted(values)
    used = vals[1:-1] if len(vals) >= 5 else vals
    return {
        "used_values": used,
        "used_mean": round(statistics.mean(used), 6),
        "used_min": round(min(used), 6),
        "used_max": round(max(used), 6),
        "used_range": round(max(used) - min(used), 6),
        "used_std": round(statistics.pstdev(used), 6) if len(used) > 1 else 0.0,
    }


def home_and_settle():
    post_gcode("G90")
    post_gcode("G28", timeout=180)
    time.sleep(3)


def build_probe_cmd(material=None, apply=False, clear=False):
    parts = ["PRTOUCH_PROBE_ZOFFSET"]
    if apply:
        parts.append("APPLY_Z_ADJUST=1")
    if clear:
        parts.append("CLEAR_NOZZLE=1")
    if material:
        parts.append(f"MATERIAL={material.upper()}")
    return " ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bed", type=int, required=True)
    ap.add_argument("--hotend", type=int, required=True)
    ap.add_argument("--material", type=str, default=None)
    ap.add_argument("--bed-soak", type=int, default=0)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-range", type=float, default=0.05)
    ap.add_argument("--retry-sets", type=int, default=2)
    args = ap.parse_args()

    if args.runs < 3:
        raise SystemExit("Usa al menos --runs 3; recomendado 5")

    material = args.material.upper() if args.material else None

    try:
        post_gcode("M117 Auto Z Start")
        post_gcode(f"M140 S{args.bed}")
        post_gcode(f"M117 Bed {args.bed}C")
        wait_for_bed(args.bed)

        if args.bed_soak > 0:
            post_gcode(f"M117 Bed soak {args.bed_soak}s")
            soak_bed(args.bed_soak, args.bed)

        post_gcode("M117 Homing")
        home_and_settle()
        post_gcode(f"M140 S{args.bed}")

        post_gcode(f"M104 S{args.hotend}")
        post_gcode(f"M117 Hotend {args.hotend}C")
        wait_for_hotend(args.hotend)
        post_gcode(f"M140 S{args.bed}")

        results = []
        passed = None

        for attempt in range(1, args.retry_sets + 1):
            post_gcode(f"M117 Z test {attempt}/{args.retry_sets}")
            post_gcode(f"M140 S{args.bed}")
            values, raw = run_measurement_set(args.runs, material=material)
            summary_all = summarize_all(values)
            summary_trim = summarize_trimmed(values)
            result = {"attempt": attempt, "raw": raw, **summary_all, **summary_trim}
            results.append(result)
            post_gcode(f"M117 Rng {result['used_range']:.3f}")
            if result["used_range"] <= args.max_range:
                passed = result
                break
            time.sleep(5)

        if passed is None:
            msg = (
                f"Auto Z abortado: sin consistencia tras {args.retry_sets} intentos. "
                f"Ultimo rango_util={results[-1]['used_range']:.4f} "
                f"valores_utiles={results[-1]['used_values']}"
            )
            post_gcode(f'RESPOND TYPE=error MSG="{msg}"')
            print(json.dumps({"ok": False, "attempts": results}, ensure_ascii=False))
            sys.exit(2)

        post_gcode("PRTOUCH_ACCURACY SAMPLES=10 PROBE_SPEED=1", timeout=LONG_GCODE_TIMEOUT)
        post_gcode(build_probe_cmd(material=material, apply=True, clear=True), timeout=LONG_GCODE_TIMEOUT)
        post_gcode("SAVE_CONFIG", timeout=120)
        print(json.dumps({"ok": True, "selected": passed, "attempts": results}, ensure_ascii=False))

    except Exception as e:
        try:
            msg = str(e).replace('"', "'")
            post_gcode(f'RESPOND TYPE=error MSG="Auto Z fallo: {msg}"')
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
