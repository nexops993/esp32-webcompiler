#!/usr/bin/env python3
"""ESP32-C3 Web Compiler v2 - Flask backend.
- Multi-file project: .ino/.h/.cpp/.c/.s/.txt bebas, nama file utama (.ino) bisa dipilih/diganti.
- Saat compile, file disalin ke sketch build bernama sesuai file utama (syarat arduino-cli),
  lalu dicompile dengan --jobs = semua CPU core.
"""
import os
import re
import uuid
import glob
import json
import time
import shutil
import threading
import subprocess
from flask import Flask, request, jsonify, send_file

BASE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(BASE, "workspace")
SRC_DIR = os.path.join(WORKSPACE, "sketch")       # file sumber milik user
GEN_DIR = os.path.join(WORKSPACE, "_gen")          # sketch siap-compile (per nama utama)
BUILD_DIR = os.path.join(WORKSPACE, "_build")      # cache build (per nama utama)
OUTPUT_BIN = os.path.join(WORKSPACE, "firmware.bin")
OUTPUT_DIR = os.path.join(WORKSPACE, "output")  # semua .bin hasil compile
PROJECT_JSON = os.path.join(WORKSPACE, "project.json")
FQBN = "esp32:esp32:esp32c3:CDCOnBoot=cdc,PartitionScheme=huge_app"
JOBS = str(os.cpu_count() or 2)
COMPILE_TIMEOUT = 600

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.(ino|h|hpp|cpp|c|cc|s|S|txt)$")

DEFAULT_SKETCH = """#include <Arduino.h>

// Contoh: blink LED bawaan ESP32-C3 (GPIO 8)
void setup() {
  Serial.begin(115200);
  pinMode(8, OUTPUT);
}

void loop() {
  digitalWrite(8, HIGH);
  delay(500);
  digitalWrite(8, LOW);
  delay(500);
}
"""

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # untuk upload .zip library
jobs = {}


def ensure_workspace():
    os.makedirs(SRC_DIR, exist_ok=True)
    os.makedirs(GEN_DIR, exist_ok=True)
    os.makedirs(BUILD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if not os.listdir(SRC_DIR):
        with open(os.path.join(SRC_DIR, "sketch.ino"), "w") as f:
            f.write(DEFAULT_SKETCH)
        save_project({"main": "sketch.ino"})
    elif not os.path.exists(PROJECT_JSON):
        inos = sorted(fn for fn in os.listdir(SRC_DIR) if fn.endswith(".ino"))
        save_project({"main": inos[0] if inos else None})


def load_project():
    try:
        with open(PROJECT_JSON) as f:
            return json.load(f)
    except Exception:
        return {"main": None}


def save_project(p):
    with open(PROJECT_JSON, "w") as f:
        json.dump(p, f)


def safe_name(name):
    name = os.path.basename((name or "").strip())
    return name if SAFE_NAME.match(name) else None


def ino_files():
    return sorted(fn for fn in os.listdir(SRC_DIR)
                  if fn.endswith(".ino") and os.path.isfile(os.path.join(SRC_DIR, fn)))


def list_files():
    proj = load_project()
    main = proj.get("main")
    out = []
    for fn in sorted(os.listdir(SRC_DIR)):
        p = os.path.join(SRC_DIR, fn)
        if os.path.isfile(p) and SAFE_NAME.match(fn):
            out.append({"name": fn, "size": os.path.getsize(p),
                        "is_main": fn == main})
    return out, main


def arduino_has_jobs_flag():
    try:
        r = subprocess.run(["arduino-cli", "compile", "--help"],
                           capture_output=True, text=True, timeout=15)
        return "--jobs" in r.stdout
    except Exception:
        return False


def core_patched():
    """Cek apakah libnet80211.a ESP32-C3 sudah di-patch
    (simbol ieee80211_raw_frame_sanity_check jadi weak)."""
    try:
        libs = glob.glob(os.path.expanduser(
            "~/.arduino15/packages/esp32/tools/esp32c3-libs/*/lib/libnet80211.a"))
        nms = glob.glob(os.path.expanduser(
            "~/.arduino15/packages/esp32/tools/esp-rv32/*/bin/riscv32-esp-elf-nm"))
        if not libs or not nms:
            return False
        r = subprocess.run([nms[0], libs[0]], capture_output=True,
                           text=True, timeout=15)
        for line in r.stdout.splitlines():
            if "ieee80211_raw_frame_sanity_check" in line:
                parts = line.split()
                return len(parts) >= 2 and parts[1] == "W"
        return False
    except Exception:
        return False


HAS_JOBS = None


def prepare_sketch(main):
    """Siapkan sketch Arduino yang valid: folder bernama <main_base>,
    berisi semua file project, file utama bernama <main_base>.ino."""
    main_base = main[:-4]
    sketch_dir = os.path.join(GEN_DIR, main_base)
    build_dir = os.path.join(BUILD_DIR, main_base)
    if os.path.isdir(sketch_dir):
        shutil.rmtree(sketch_dir)
    os.makedirs(sketch_dir, exist_ok=True)
    os.makedirs(build_dir, exist_ok=True)
    for fn in os.listdir(SRC_DIR):
        src = os.path.join(SRC_DIR, fn)
        if not os.path.isfile(src) or not SAFE_NAME.match(fn):
            continue
        dst_name = main_base + ".ino" if fn == main else fn
        shutil.copyfile(src, os.path.join(sketch_dir, dst_name))
    return sketch_dir, build_dir, main_base


def run_compile(job_id, main, clean=False):
    job = jobs[job_id]
    log = job["log"]
    t0 = time.time()
    try:
        sketch_dir, build_dir, main_base = prepare_sketch(main)
        global HAS_JOBS
        if HAS_JOBS is None:
            HAS_JOBS = arduino_has_jobs_flag()
        cmd = ["arduino-cli", "compile", "--fqbn", FQBN]
        if HAS_JOBS:
            cmd += ["--jobs", JOBS]
        if clean:
            cmd += ["--clean"]
        cmd += ["--build-path", build_dir, sketch_dir]
        log.append("$ " + " ".join(cmd) + "\n")
        log.append(f"[info] file utama: {main} | parallel jobs: {JOBS} core"
                   + (" | CLEAN BUILD (--clean)\n" if clean else "\n"))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            log.append(line)
        proc.wait(timeout=COMPILE_TIMEOUT)
        elapsed = time.time() - t0
        job["elapsed"] = round(elapsed, 1)
        if proc.returncode == 0:
            # Kumpulkan SEMUA .bin: bootloader, partitions, utama, merged
            for old in glob.glob(os.path.join(OUTPUT_DIR, "*.bin")):
                os.remove(old)
            bins_info = []
            for b in sorted(glob.glob(os.path.join(build_dir, "*.bin"))):
                bn = os.path.basename(b)
                dst = os.path.join(OUTPUT_DIR, bn)
                shutil.copyfile(b, dst)
                bins_info.append({"name": bn, "size": os.path.getsize(dst)})
            if not bins_info:
                job.update(status="done", success=False,
                           error="Compile OK tapi file .bin tidak ditemukan.")
                return
            # File utama = <main>.ino.bin (seperti Export Compiled Binary),
            # fallback ke merged bila tidak ada
            main_cands = [x for x in bins_info
                          if x["name"] == main_base + ".ino.bin"]
            if not main_cands:
                main_cands = [x for x in bins_info if "merged" in x["name"]]
            if not main_cands:
                main_cands = bins_info
            main_bin = main_cands[0]
            shutil.copyfile(os.path.join(OUTPUT_DIR, main_bin["name"]),
                            OUTPUT_BIN)
            size = main_bin["size"]
            job.update(status="done", success=True, bin_size=size,
                       bin_name=main_bin["name"], bins=bins_info)
            log.append(f"\n[SUCCESS] {elapsed:.1f}s — {len(bins_info)} file .bin "
                       f"tersedia, utama: {main_bin['name']} ({size/1024:.1f} KB)\n")
        else:
            job.update(status="done", success=False,
                       error=f"arduino-cli exit code {proc.returncode}")
            log.append(f"\n[FAILED] exit code {proc.returncode}\n")
    except subprocess.TimeoutExpired:
        job.update(status="done", success=False,
                   error="Compile timeout (>10 menit).")
    except Exception as e:
        job.update(status="done", success=False, error=str(e))


# ---------- routes ----------

@app.route("/")
def index():
    return send_file(os.path.join(BASE, "index.html"))


@app.route("/api/info")
def info():
    return jsonify({"fqbn": FQBN, "jobs": JOBS, "board": "ESP32-C3",
                    "patched": core_patched()})


@app.route("/api/files")
def api_list():
    files, main = list_files()
    return jsonify({"files": files, "main": main})


@app.route("/api/file")
def api_get():
    name = safe_name(request.args.get("name", ""))
    if not name:
        return jsonify({"error": "nama file tidak valid"}), 400
    p = os.path.join(SRC_DIR, name)
    if not os.path.isfile(p):
        return jsonify({"error": "file tidak ditemukan"}), 404
    with open(p, "r", errors="replace") as f:
        return jsonify({"name": name, "content": f.read()})


@app.route("/api/file", methods=["POST"])
def api_save():
    data = request.get_json(force=True, silent=True) or {}
    name = safe_name(data.get("name", ""))
    if not name:
        return jsonify({"error": "nama file tidak valid"}), 400
    content = data.get("content", "")
    if len(content) > 2_000_000:
        return jsonify({"error": "file terlalu besar (maks 2MB)"}), 400
    with open(os.path.join(SRC_DIR, name), "w") as f:
        f.write(content)
    return jsonify({"ok": True})


@app.route("/api/file/new", methods=["POST"])
def api_new():
    data = request.get_json(force=True, silent=True) or {}
    name = safe_name(data.get("name", ""))
    if not name:
        return jsonify({"error": "nama tidak valid (contoh: wifi.h, main.ino)"}), 400
    p = os.path.join(SRC_DIR, name)
    if os.path.exists(p):
        return jsonify({"error": "file sudah ada"}), 400
    with open(p, "w") as f:
        f.write("// file baru\n")
    if name.endswith(".ino") and not load_project().get("main"):
        save_project({"main": name})
    return jsonify({"ok": True})


@app.route("/api/file/rename", methods=["POST"])
def api_rename():
    data = request.get_json(force=True, silent=True) or {}
    old = safe_name(data.get("old", ""))
    new = safe_name(data.get("new", ""))
    if not old or not new:
        return jsonify({"error": "nama tidak valid"}), 400
    sp, dp = os.path.join(SRC_DIR, old), os.path.join(SRC_DIR, new)
    if not os.path.isfile(sp):
        return jsonify({"error": "file tidak ditemukan"}), 404
    if os.path.exists(dp):
        return jsonify({"error": "nama tujuan sudah dipakai"}), 400
    os.rename(sp, dp)
    proj = load_project()
    if proj.get("main") == old:
        proj["main"] = new
        save_project(proj)
    return jsonify({"ok": True})


@app.route("/api/file/main", methods=["POST"])
def api_set_main():
    data = request.get_json(force=True, silent=True) or {}
    name = safe_name(data.get("name", ""))
    if not name or not name.endswith(".ino"):
        return jsonify({"error": "file utama harus .ino"}), 400
    if not os.path.isfile(os.path.join(SRC_DIR, name)):
        return jsonify({"error": "file tidak ditemukan"}), 404
    save_project({"main": name})
    return jsonify({"ok": True})


@app.route("/api/file", methods=["DELETE"])
def api_delete():
    name = safe_name(request.args.get("name", ""))
    if not name:
        return jsonify({"error": "nama file tidak valid"}), 400
    p = os.path.join(SRC_DIR, name)
    if os.path.isfile(p):
        os.remove(p)
    proj = load_project()
    if proj.get("main") == name:
        rest = ino_files()
        save_project({"main": rest[0] if rest else None})
    return jsonify({"ok": True})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    saved = []
    for key in request.files:
        for fs in request.files.getlist(key):
            name = safe_name(fs.filename)
            if not name:
                continue
            fs.save(os.path.join(SRC_DIR, name))
            saved.append(name)
    if not saved:
        return jsonify({"error": "tidak ada file valid yang diupload"}), 400
    proj = load_project()
    if not proj.get("main"):
        inos = [n for n in saved if n.endswith(".ino")] or ino_files()
        if inos:
            save_project({"main": inos[0]})
    return jsonify({"ok": True, "saved": saved})


@app.route("/api/compile", methods=["POST"])
def api_compile():
    for j in jobs.values():
        if j["status"] == "running":
            return jsonify({"error": "masih ada compile yang berjalan"}), 409
    data = request.get_json(force=True, silent=True) or {}
    clean = bool(data.get("clean"))
    main = load_project().get("main")
    if not main or not os.path.isfile(os.path.join(SRC_DIR, main)):
        return jsonify({"error": "pilih file utama (.ino) dulu"}), 400
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"status": "running", "log": [], "success": None,
                    "error": None, "elapsed": None, "bin_size": None,
                    "main": main, "clean": clean}
    if len(jobs) > 10:
        for k in list(jobs)[:len(jobs) - 10]:
            del jobs[k]
    threading.Thread(target=run_compile, args=(job_id, main, clean),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/compile/<job_id>")
def api_compile_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job tidak ditemukan"}), 404
    return jsonify({"status": job["status"], "log": "".join(job["log"]),
                    "success": job["success"], "error": job["error"],
                    "elapsed": job["elapsed"], "bin_size": job["bin_size"],
                    "bin_name": job.get("bin_name"), "main": job.get("main"),
                    "clean": job.get("clean", False),
                    "bins": job.get("bins", [])})


@app.route("/api/download")
def api_download():
    if not os.path.isfile(OUTPUT_BIN):
        return jsonify({"error": "belum ada hasil compile"}), 404
    return send_file(OUTPUT_BIN, as_attachment=True,
                     download_name="firmware-esp32c3.bin")


SAFE_BIN = re.compile(r"^[A-Za-z0-9_.-]+\.bin$")


@app.route("/api/download/<fname>")
def api_download_one(fname):
    if not SAFE_BIN.match(fname or ""):
        return jsonify({"error": "nama file tidak valid"}), 400
    p = os.path.join(OUTPUT_DIR, os.path.basename(fname))
    if not os.path.isfile(p):
        return jsonify({"error": "file tidak ditemukan"}), 404
    return send_file(p, as_attachment=True, download_name=fname)


# ---------------------------------------------------------------- library ----
LIB_TASKS = {}


def run_lib_task(tid, action, name=None, zip_path=None):
    t = LIB_TASKS[tid]
    log = t["log"]
    try:
        if action == "install" and zip_path:
            cmd = ["arduino-cli", "lib", "install", "--zip-path", zip_path]
        elif action == "install":
            cmd = ["arduino-cli", "lib", "install", name]
        else:
            cmd = ["arduino-cli", "lib", "uninstall", name]
        log.append("$ " + " ".join(cmd) + "\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            log.append(line)
        proc.wait()
        t["status"] = "done"
        t["success"] = proc.returncode == 0
        if proc.returncode != 0:
            t["error"] = "exit code %d" % proc.returncode
    except Exception as e:
        t["status"] = "done"
        t["success"] = False
        t["error"] = str(e)
    finally:
        if zip_path and os.path.isfile(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass


def _new_lib_task(label):
    tid = uuid.uuid4().hex[:8]
    LIB_TASKS[tid] = {"status": "running", "log": [], "success": None,
                      "error": None, "label": label}
    if len(LIB_TASKS) > 20:
        for k in list(LIB_TASKS)[:len(LIB_TASKS) - 20]:
            del LIB_TASKS[k]
    return tid


@app.route("/api/libraries")
def api_libraries():
    try:
        p = subprocess.run(["arduino-cli", "lib", "list", "--format", "json"],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(p.stdout or "{}")
        raw = data.get("installed_libraries") or data.get("installed") or []
        out = []
        for e in raw:
            l = e.get("library", e) if isinstance(e, dict) else {}
            out.append({"name": l.get("name"), "version": l.get("version"),
                        "author": l.get("author")})
        out.sort(key=lambda x: (x["name"] or "").lower())
        return jsonify({"libraries": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/libraries/search")
def api_lib_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"libraries": []})
    try:
        p = subprocess.run(["arduino-cli", "lib", "search", q,
                            "--format", "json"],
                           capture_output=True, text=True, timeout=90)
        data = json.loads(p.stdout or "{}")
        out = []
        for l in (data.get("libraries") or [])[:20]:
            latest = l.get("latest") or {}
            para = (l.get("paragraph") or "").strip()
            out.append({"name": l.get("name"),
                        "version": latest.get("version"),
                        "paragraph": para[:140]})
        return jsonify({"libraries": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/libraries/install", methods=["POST"])
def api_lib_install():
    name = None
    zip_path = None
    if request.files and "zip" in request.files:
        f = request.files["zip"]
        if not (f.filename or "").lower().endswith(".zip"):
            return jsonify({"error": "file harus .zip"}), 400
        zip_path = os.path.join("/tmp", "lib_" + uuid.uuid4().hex[:8] + ".zip")
        f.save(zip_path)
        label = f.filename
    else:
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "nama library kosong"}), 400
        label = name
    tid = _new_lib_task(label)
    threading.Thread(target=run_lib_task, args=(tid, "install", name, zip_path),
                     daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/libraries/uninstall", methods=["POST"])
def api_lib_uninstall():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nama library kosong"}), 400
    tid = _new_lib_task(name)
    threading.Thread(target=run_lib_task, args=(tid, "uninstall", name, None),
                     daemon=True).start()
    return jsonify({"task_id": tid})


@app.route("/api/libtask/<tid>")
def api_libtask(tid):
    t = LIB_TASKS.get(tid)
    if not t:
        return jsonify({"error": "task tidak ditemukan"}), 404
    return jsonify(t)


if __name__ == "__main__":
    ensure_workspace()
    app.run(host="127.0.0.1", port=8765, threaded=True)
