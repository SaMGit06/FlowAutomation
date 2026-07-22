from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import winreg
import threading
import time
import os
import json
import queue
import uuid
from datetime import datetime


# ── CONFIG ─────────────────────────────────────────────────────────────────────
def get_default_browser():

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        ) as key:
            progid = winreg.QueryValueEx(key, "ProgId")[0]

        browsers = {
            "ChromeHTML": (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                os.path.join(os.environ["LOCALAPPDATA"], "Google", "Chrome", "User Data", "Default"),
            ),
            "MSEdgeHTM": (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Edge", "User Data", "Default"),
            ),
            "BraveHTML": (
                r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                os.path.join(os.environ["LOCALAPPDATA"], "BraveSoftware", "Brave-Browser", "User Data", "Default"),
            ),
            "OperaStable": (
                os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Opera", "opera.exe"),
                os.path.join(os.environ["APPDATA"], "Opera Software", "Opera Stable"),
            ),
            "VivaldiHTM": (
                r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
                os.path.join(os.environ["LOCALAPPDATA"], "Vivaldi", "User Data", "Default"),
            ),
        }

        return browsers.get(progid, (None, None))

    except Exception:
        return (None, None)

BROWSER_PATH, PROFILE_PATH = get_default_browser()


PROJECT_URL   = "https://labs.google/fx/tools/flow"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "Flow")
LOGS_DIR = os.path.join(DOWNLOADS_DIR, "LOGS")
ANALYTICS_FILE = os.path.join(DOWNLOADS_DIR, "analytics.json")
MAX_RETRY     = 3

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ── APP ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── SHARED STATE ──────────────────────────────────────────────────────────────
stop_event   = threading.Event()
log_lock     = threading.Lock()
log_buffer   = []          # Rolling buffer of all log entries
sse_clients  = []          # List of queues — one per connected SSE client

automation_state = {
    "running":    False,
    "current":    0,
    "total":      0,
    "completed":  0,
    "failed":     0,
    "session_id": None,
    "started_at": None,
}

# ── LOGGING ───────────────────────────────────────────────────────────────────
_log_file = None

def _get_log_file():
    global _log_file
    if _log_file is None or _log_file.closed:
        fname = f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        _log_file = open(os.path.join(LOGS_DIR, fname), "a", encoding="utf-8")
    return _log_file

def add_log(message, level="info"):
    """
    Log to: console + in-memory buffer + log file on disk + all SSE clients.
    level: info | ok | warn | err | event
    """
    entry = {
        "time":  time.strftime("%H:%M:%S"),
        "msg":   message,
        "level": level,
        "ts":    time.time(),
    }

    # Memory
    with log_lock:
        log_buffer.append(entry)
        if len(log_buffer) > 300:
            log_buffer.pop(0)

    # Disk
    try:
        f = _get_log_file()
        f.write(f"[{entry['time']}] [{level.upper():5}] {message}\n")
        f.flush()
    except Exception as e:
        print(f"Log-file error: {e}")

    # SSE broadcast
    dead = []
    for q in list(sse_clients):
        try:
            q.put_nowait(entry)
        except queue.Full:
            dead.append(q)
        except Exception:
            dead.append(q)
    for q in dead:
        try: sse_clients.remove(q)
        except ValueError: pass

    print(f"[{entry['time']}] {message}")

# ── BROADCAST TYPED EVENT ─────────────────────────────────────────────────────
def broadcast_event(event_type, data):
    """Push a structured event to all SSE clients (used for UI state updates)."""
    entry = {
        "time":  time.strftime("%H:%M:%S"),
        "msg":   f"[{event_type}]",
        "level": "event",
        "event": event_type,
        "data":  data,
        "ts":    time.time(),
    }
    for q in list(sse_clients):
        try:
            q.put_nowait(entry)
        except Exception:
            pass

# ── ANALYTICS ─────────────────────────────────────────────────────────────────
def load_analytics():
    if os.path.exists(ANALYTICS_FILE):
        try:
            with open(ANALYTICS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "sessions":                [],
        "total_videos_requested":  0,
        "total_videos_completed":  0,
        "total_sessions":          0,
        "total_retries":           0,
    }

def save_analytics(data):
    try:
        with open(ANALYTICS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        add_log(f"Analytics save error: {e}", "err")

# ── CORE AUTOMATION ───────────────────────────────────────────────────────────
def _generate_single_video(page, video, idx):
    """
    Automate one video prompt in Brave.
    Raises Exception on failure so the retry loop can catch it.
    """
    page.mouse.click(448, 620)
    page.wait_for_timeout(1000)

    for char in video.get("characters", []):
        page.keyboard.type(f"@{char}")
        page.wait_for_timeout(1500)
        page.keyboard.press("Enter")
        page.keyboard.press("Space")

    page.keyboard.type(video["prompt"])
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)
    page.keyboard.press("Escape")

    add_log(f"  ⏳ Waiting 80 s for generation...", "info")
    page.wait_for_timeout(80000)

    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")
    page.wait_for_timeout(5000)

    add_log(f"  ⬇  Downloading video {idx}...", "info")
    with page.expect_download() as dl_info:
        page.mouse.click(1024, 34)

    download = dl_info.value
    filename = f"video_{int(time.time())}.mp4"
    save_path = os.path.join(DOWNLOADS_DIR, filename)
    download.save_as(save_path)
    add_log(f"  💾 Saved → {filename}", "ok")

    page.go_back()
    page.wait_for_timeout(5000)


def run_automation(videos, session_id, user_name="unknown"):
    from playwright.sync_api import sync_playwright

    analytics = load_analytics()
    session_record = {
        "id":               session_id,
        "user":             user_name,
        "started_at":       datetime.now().isoformat(),
        "ended_at":         None,
        "videos_requested": len(videos),
        "videos_completed": 0,
        "videos_failed":    0,
        "retries":          0,
        "stopped_early":    False,
    }

    automation_state.update({
        "running":    True,
        "current":    0,
        "total":      len(videos),
        "completed":  0,
        "failed":     0,
        "session_id": session_id,
        "started_at": datetime.now().isoformat(),
    })

    add_log(f"🚀 Session {session_id[:8]} — {len(videos)} video(s) queued  |  user: {user_name}", "info")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_PATH,
                executable_path=BROWSER_PATH,
                headless=False,
                accept_downloads=True,
            )
            page = context.new_page()
            add_log("🌐 Browser launched — loading Flow project...", "info")
            page.goto(PROJECT_URL)
            page.wait_for_timeout(5000)
            add_log("✓ Flow project loaded", "ok")

            for idx, video in enumerate(videos, start=1):

                # ── STOP CHECK ────────────────────────────────────────────────
                if stop_event.is_set():
                    add_log(f"⛔ Stop requested before video {idx}", "warn")
                    session_record["stopped_early"] = True
                    broadcast_event("video_stopped", {"vid_id": video.get("id"), "index": idx})
                    break

                automation_state["current"] = idx
                add_log(f"▶  Video {idx}/{len(videos)} — starting...", "info")
                broadcast_event("video_start", {
                    "vid_id": video.get("id"),
                    "index":  idx,
                    "total":  len(videos),
                })

                # ── RETRY LOOP ────────────────────────────────────────────────
                success = False
                for attempt in range(1, MAX_RETRY + 1):
                    if stop_event.is_set():
                        break
                    if attempt > 1:
                        add_log(f"  🔁 Retry {attempt}/{MAX_RETRY} for video {idx}", "warn")
                        session_record["retries"]       += 1
                        analytics["total_retries"]      += 1
                        broadcast_event("video_retry", {
                            "vid_id":  video.get("id"),
                            "attempt": attempt,
                        })
                        page.wait_for_timeout(3000)
                    try:
                        _generate_single_video(page, video, idx)
                        success = True
                        break
                    except Exception as e:
                        add_log(f"  ✗ Attempt {attempt} failed: {e}", "err")

                # ── RESULT ────────────────────────────────────────────────────
                if success:
                    automation_state["completed"]       += 1
                    session_record["videos_completed"]  += 1
                    pct = int(automation_state["completed"] / len(videos) * 100)
                    add_log(f"✅ Video {idx} done  ({pct}% complete)", "ok")
                    broadcast_event("video_done", {
                        "vid_id":    video.get("id"),
                        "index":     idx,
                        "total":     len(videos),
                        "completed": automation_state["completed"],
                        "pct":       pct,
                    })
                else:
                    automation_state["failed"]         += 1
                    session_record["videos_failed"]    += 1
                    if not stop_event.is_set():
                        add_log(f"❌ Video {idx} FAILED after {MAX_RETRY} attempts", "err")
                    broadcast_event("video_failed", {
                        "vid_id": video.get("id"),
                        "index":  idx,
                    })

            context.close()

        # ── ALL DONE ──────────────────────────────────────────────────────────
        if not stop_event.is_set():
            add_log(
                f"🎉 ALL DONE!  {automation_state['completed']}/{len(videos)} completed"
                + (f",  {automation_state['failed']} failed" if automation_state["failed"] else ""),
                "ok",
            )
            broadcast_event("all_done", {
                "completed": automation_state["completed"],
                "failed":    automation_state["failed"],
                "total":     len(videos),
            })

    except Exception as e:
        add_log(f"💥 Fatal error: {e}", "err")
        broadcast_event("fatal_error", {"error": str(e)})

    finally:
        automation_state["running"] = False
        automation_state["current"] = 0
        session_record["ended_at"]  = datetime.now().isoformat()

        analytics["sessions"].append(session_record)
        analytics["total_videos_requested"] += session_record["videos_requested"]
        analytics["total_videos_completed"] += session_record["videos_completed"]
        analytics["total_sessions"]         += 1
        save_analytics(analytics)

        stop_event.clear()
        add_log("🏁 Automation thread finished.", "info")

# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/generate", methods=["POST"])
def generate():
    if automation_state["running"]:
        return jsonify({"error": "Already running — stop current session first"}), 409

    data       = request.json or {}
    videos     = data.get("videos", [])
    user_name  = data.get("user", "unknown")
    session_id = str(uuid.uuid4())

    if not videos:
        return jsonify({"error": "No videos provided"}), 400

    stop_event.clear()
    threading.Thread(
        target=run_automation,
        args=(videos, session_id, user_name),
        daemon=True,
    ).start()

    return jsonify({
        "status":     "started",
        "session_id": session_id,
        "total":      len(videos),
    })


@app.route("/stop", methods=["POST"])
def stop():
    if not automation_state["running"]:
        return jsonify({"status": "not_running"})
    stop_event.set()
    add_log("⛔ STOP SIGNAL received — finishing current video then stopping...", "warn")
    return jsonify({"status": "stopping"})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "running":    automation_state["running"],
        "current":    automation_state["current"],
        "total":      automation_state["total"],
        "completed":  automation_state["completed"],
        "failed":     automation_state["failed"],
        "session_id": automation_state["session_id"],
        "pct": int(automation_state["completed"] / automation_state["total"] * 100)
               if automation_state["total"] > 0 else 0,
    })


@app.route("/logs", methods=["GET"])
def get_logs():
    since = float(request.args.get("since", 0))
    with log_lock:
        result = [l for l in log_buffer if l["ts"] > since]
    return jsonify({"logs": result})


@app.route("/stream")
def stream():
    """Server-Sent Events endpoint — push logs + events in real time."""
    def generate():
        q = queue.Queue(maxsize=200)
        sse_clients.append(q)
        try:
            # Replay last 50 entries so the page is instantly populated
            with log_lock:
                recent = list(log_buffer[-50:])
            for entry in recent:
                yield f"data: {json.dumps(entry)}\n\n"

            while True:
                try:
                    entry = q.get(timeout=20)
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    yield 'data: {"ping":true}\n\n'   # keepalive
        except GeneratorExit:
            pass
        finally:
            try:
                sse_clients.remove(q)
            except ValueError:
                pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering":"no",
            "Connection":       "keep-alive",
        },
    )


@app.route("/analytics", methods=["GET"])
def get_analytics():
    data = load_analytics()
    data["live"] = {
        "running":   automation_state["running"],
        "current":   automation_state["current"],
        "total":     automation_state["total"],
        "completed": automation_state["completed"],
        "failed":    automation_state["failed"],
    }
    return jsonify(data)


if __name__ == "__main__":
    add_log("▶  SAM's Flow Automation Server  |  http://0.0.0.0:5000", "ok")
    app.run(host="0.0.0.0", port=5000, threaded=True)
