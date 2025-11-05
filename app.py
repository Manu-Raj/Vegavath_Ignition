import os
import json
import zipfile
import shutil
import base64
import requests
import bcrypt
import threading
import uuid
import queue
import time

from flask import Flask, render_template, request, session, redirect, url_for, jsonify, Response
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(32)

# Load teams (bcrypt-hashed PINs)
with open("teams.json", "r") as f:
    teams = json.load(f)

# Load upload lock state
UPLOAD_STATE_FILE = "uploads_state.json"
if os.path.exists(UPLOAD_STATE_FILE):
    with open(UPLOAD_STATE_FILE, "r") as f:
        upload_state = json.load(f)
else:
    upload_state = {}

def save_upload_state():
    with open(UPLOAD_STATE_FILE, "w") as f:
        json.dump(upload_state, f, indent=2)

# GitHub Config
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = "Manu-Raj/Test"  # Your repository


# ===================== GitHub Upload Helper =====================

def github_upload(local_file_path, github_repo_path):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_repo_path}"
    with open(local_file_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    get_res = requests.get(url, headers=headers)
    sha = get_res.json()["sha"] if get_res.status_code == 200 else None

    payload = {"message": f"Upload {github_repo_path}", "content": content}
    if sha: payload["sha"] = sha

    put_res = requests.put(url, json=payload, headers=headers)
    return (put_res.status_code in (200, 201), put_res.status_code, put_res.text)


def safe_relpath(path, start):
    return os.path.relpath(path, start).replace("\\", "/")


# ===================== SSE Upload Processing =====================

upload_queues = {}
upload_meta = {}

def json_event(event, data):
    import json as _json
    return (event, _json.dumps(data))


def process_upload(upload_id, team, temp_dir):
    q = upload_queues.get(upload_id)
    if not q:
        return

    try:
        all_files = []
        for root, _, files in os.walk(temp_dir):
            for name in files:
                all_files.append(os.path.join(root, name))

        total = len(all_files)
        upload_meta[upload_id] = {"total": total, "current": 0, "team": team}

        q.put(json_event("info", f"{total} files to upload."))

        for i, file_path in enumerate(all_files, start=1):
            rel = safe_relpath(file_path, temp_dir)
            gh_path = f"submissions/{team}/{rel}"

            q.put(json_event("upload_start", {"file": gh_path, "index": i, "total": total}))
            ok, status, resp = github_upload(file_path, gh_path)

            if ok:
                q.put(json_event("upload_done", {"file": gh_path, "status": status}))
            else:
                q.put(json_event("upload_error", {"file": gh_path, "status": status, "response": resp}))

            upload_meta[upload_id]["current"] = i
            time.sleep(0.05)

        q.put(json_event("finished", "All uploads completed."))

    finally:
        time.sleep(1)
        shutil.rmtree(temp_dir, ignore_errors=True)
        q.put(json_event("closed", "Upload thread ended."))


# ===================== Routes =====================

@app.route("/", methods=["GET", "POST"])
def verify():
    if request.method == "POST":
        team = request.form.get("team")
        pin = request.form.get("pin", "").encode()

        stored_hash = teams.get(team)
        if stored_hash and bcrypt.checkpw(pin, stored_hash.encode()):

            # Secret Administrator disguised as team
            if team == "VEGAVATH ADS":
                session["admin"] = True
                return redirect(url_for("admin_panel"))

            # Check if team already submitted
            if upload_state.get(team):
                return render_template("verify.html", teams=teams.keys(),
                                       error="⚠️ Your team already submitted. Contact admin to unlock.")

            session["team"] = team
            return redirect(url_for("upload"))

        return render_template("verify.html", teams=teams.keys(), error="❌ Wrong PIN")

    return render_template("verify.html", teams=teams.keys())


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "team" not in session:
        return redirect(url_for("verify"))

    team = session["team"]

    if request.method == "POST":
        upload_id = str(uuid.uuid4())
        temp_dir = f"temp_{team}_{upload_id}"
        os.makedirs(temp_dir, exist_ok=True)

        file = request.files["file"]
        zip_path = os.path.join(temp_dir, "sub.zip")
        file.save(zip_path)

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(temp_dir)
        os.remove(zip_path)

        upload_state[team] = True
        save_upload_state()

        q = queue.Queue()
        upload_queues[upload_id] = q
        threading.Thread(target=process_upload, args=(upload_id, team, temp_dir), daemon=True).start()

        session.clear()
        return redirect(url_for("success", upload_id=upload_id))

    return render_template("upload.html", team=team)


@app.route("/success")
def success():
    return render_template("success.html", upload_id=request.args.get("upload_id"))


@app.route("/events/<upload_id>")
def events(upload_id):
    if upload_id not in upload_queues:
        return "Invalid", 404

    q = upload_queues[upload_id]

    def stream():
        while True:
            event, data = q.get()
            yield f"event: {event}\ndata: {data}\n\n"
            if event == "closed":
                break

    return Response(stream(), mimetype="text/event-stream")


# ===================== Admin =====================

@app.route("/admin")
def admin_panel():
    if "admin" not in session:
        return redirect(url_for("verify"))

    return render_template("admin.html", upload_state=upload_state)


@app.route("/admin/reset/<team>", methods=["POST"])
def reset_team(team):
    if "admin" not in session:
        return redirect(url_for("verify"))
    upload_state[team] = False
    save_upload_state()
    return redirect(url_for("admin_panel"))


# ===================== App Start =====================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
