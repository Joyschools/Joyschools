import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
MEDIA_DIR = BASE_DIR / "media"
DB_PATH = Path(os.environ.get("DATABASE_PATH", INSTANCE_DIR / "tms.db"))

INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024

ALLOWED_VIDEO = {"mp4", "webm", "m4v", "mov", "mkv"}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('movie','tv')),
            title TEXT NOT NULL,
            year INTEGER,
            duration TEXT DEFAULT '',
            rating TEXT DEFAULT '',
            genres TEXT DEFAULT '',
            description TEXT DEFAULT '',
            poster TEXT DEFAULT '',
            backdrop TEXT DEFAULT '',
            featured INTEGER DEFAULT 0,
            source_type TEXT DEFAULT 'local',
            source_url TEXT DEFAULT '',
            local_file TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            name TEXT NOT NULL,
            duration TEXT DEFAULT '',
            source_type TEXT DEFAULT 'local',
            source_url TEXT DEFAULT '',
            local_file TEXT DEFAULT '',
            FOREIGN KEY(title_id) REFERENCES titles(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            accuracy_m REAL,
            location_consent INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            user_agent TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS api_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            search_path TEXT DEFAULT '',
            info_path TEXT DEFAULT '',
            seasons_path TEXT DEFAULT '',
            episodes_path TEXT DEFAULT '',
            stream_path TEXT DEFAULT '',
            download_path TEXT DEFAULT '',
            subtitles_path TEXT DEFAULT '',
            search_param TEXT DEFAULT 'query',
            headers_json TEXT DEFAULT '{}',
            search_results_path TEXT DEFAULT '',
            id_key TEXT DEFAULT '',
            title_key TEXT DEFAULT '',
            poster_key TEXT DEFAULT '',
            backdrop_key TEXT DEFAULT '',
            year_key TEXT DEFAULT '',
            type_key TEXT DEFAULT '',
            description_key TEXT DEFAULT '',
            stream_url_key TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'ERROR',
            source TEXT NOT NULL DEFAULT 'system',
            error_type TEXT NOT NULL DEFAULT 'Exception',
            message TEXT NOT NULL,
            route TEXT DEFAULT '',
            status INTEGER DEFAULT 500,
            request_id TEXT DEFAULT ''
        );
        """
    )
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
    if count == 0:
        seeds = [
            ("movie", "Big Buck Bunny", 2008, "9 min", "7.5", "Animation, Comedy", "A playful short film created by the Blender Foundation and released under a Creative Commons license.", "/static/posters/bigbuck.svg", "/static/posters/bigbuck.svg", 1, "remote", "https://storage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4", ""),
            ("movie", "Elephants Dream", 2006, "11 min", "7.0", "Animation, Fantasy", "An atmospheric open movie from the Blender Foundation, included here as a demo stream.", "/static/posters/elephants.svg", "/static/posters/elephants.svg", 0, "remote", "https://storage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4", ""),
            ("movie", "For Bigger Blazes", 2013, "15 sec", "6.3", "Demo, Short", "A lightweight playback test item for checking the player on phone and desktop.", "/static/posters/blaze.svg", "/static/posters/blaze.svg", 0, "remote", "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4", ""),
            ("tv", "Open Screen Stories", 2026, "", "8.1", "Drama, Anthology", "A demo series container. Add your own authorized episodes through the catalogue manager.", "/static/posters/series.svg", "/static/posters/series.svg", 0, "local", "", ""),
        ]
        conn.executemany(
            """INSERT INTO titles(kind,title,year,duration,rating,genres,description,poster,backdrop,featured,source_type,source_url,local_file,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [s + (now_iso(),) for s in seeds],
        )
        tv_id = conn.execute("SELECT id FROM titles WHERE title='Open Screen Stories'").fetchone()[0]
        conn.executemany(
            """INSERT INTO episodes(title_id,season,episode,name,duration,source_type,source_url,local_file)
               VALUES(?,?,?,?,?,?,?,?)""",
            [
                (tv_id, 1, 1, "First Light", "4 min", "remote", "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4", ""),
                (tv_id, 1, 2, "The Long Way Home", "1 min", "remote", "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4", ""),
            ],
        )
        conn.commit()
    conn.close()


@app.before_request
def bootstrap():
    init_db()
    g.request_id = request.headers.get("X-Request-ID") or os.urandom(8).hex()
    if session.get("user_id"):
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        try:
            g.db.execute("UPDATE users SET last_seen=? WHERE id=?", (now_iso(), session["user_id"]))
            g.db.commit()
        finally:
            g.db.close()
            g.pop("db", None)


def log_system_error(message, source="system", error_type="Exception", status=500, severity="ERROR"):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO system_errors(created_at,severity,source,error_type,message,route,status,request_id) VALUES(?,?,?,?,?,?,?,?)",
            (now_iso(), severity, source, error_type, str(message)[:3000], request.path[:500], int(status), getattr(g, "request_id", "")),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


@app.errorhandler(HTTPException)
def handle_http_error(exc):
    if exc.code and exc.code >= 400:
        log_system_error(exc.description, source="http", error_type=exc.name or "HTTPException", status=exc.code, severity="WARNING" if exc.code < 500 else "ERROR")
    return exc


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    log_system_error(exc, source="app", error_type=type(exc).__name__, status=500)
    if request.path.startswith("/api/") or request.path.startswith("/api-"):
        return jsonify({"error": "TM & S encountered a system error.", "request_id": getattr(g, "request_id", "")}), 500
    return (f"TM & S encountered a system error. Request ID: {getattr(g, 'request_id', '')}", 500)


def title_row(title_id):
    row = db().execute("SELECT * FROM titles WHERE id=?", (title_id,)).fetchone()
    if not row:
        abort(404)
    return row


def as_dict(row):
    return dict(row) if row else None


@app.route("/")
def home():
    featured = db().execute("SELECT * FROM titles WHERE featured=1 ORDER BY id DESC LIMIT 1").fetchone()
    popular = db().execute("SELECT * FROM titles ORDER BY featured DESC, rating DESC, id DESC LIMIT 12").fetchall()
    movies = db().execute("SELECT * FROM titles WHERE kind='movie' ORDER BY id DESC LIMIT 12").fetchall()
    tv = db().execute("SELECT * FROM titles WHERE kind='tv' ORDER BY id DESC LIMIT 12").fetchall()
    api = active_api()
    return render_template("home.html", featured=featured, popular=popular, movies=movies, tv=tv, api_connected=bool(api))


@app.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        try:
            latitude = float(request.form.get("latitude")) if request.form.get("latitude") else None
            longitude = float(request.form.get("longitude")) if request.form.get("longitude") else None
            accuracy = float(request.form.get("accuracy")) if request.form.get("accuracy") else None
        except ValueError:
            latitude = longitude = accuracy = None
        consent = 1 if request.form.get("location_consent") == "1" and latitude is not None and longitude is not None else 0
        if not phone:
            return render_template("join.html", error="Phone number is required."), 400
        conn = db()
        existing = conn.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        stamp = now_iso()
        if existing:
            conn.execute(
                "UPDATE users SET name=?, latitude=?, longitude=?, accuracy_m=?, location_consent=?, last_seen=?, user_agent=? WHERE id=?",
                (name, latitude, longitude, accuracy, consent, stamp, request.user_agent.string[:500], existing["id"]),
            )
            user_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO users(name,phone,latitude,longitude,accuracy_m,location_consent,joined_at,last_seen,user_agent)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (name, phone, latitude, longitude, accuracy, consent, stamp, stamp, request.user_agent.string[:500]),
            )
            user_id = cur.lastrowid
        conn.commit()
        session["user_id"] = user_id
        return redirect(url_for("home"))
    return render_template("join.html", error=None)


@app.route("/leave")
def leave():
    session.pop("user_id", None)
    return redirect(url_for("home"))


@app.route("/browse")
def browse():
    kind = request.args.get("kind", "all")
    query = request.args.get("q", "").strip()
    genre = request.args.get("genre", "").strip()
    params = []
    where = []
    if kind in {"movie", "tv"}:
        where.append("kind=?")
        params.append(kind)
    if query:
        where.append("(title LIKE ? OR description LIKE ? OR genres LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like])
    if genre:
        where.append("genres LIKE ?")
        params.append(f"%{genre}%")
    sql = "SELECT * FROM titles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY featured DESC, year DESC, id DESC"
    items = db().execute(sql, params).fetchall()

    api_results = []
    api_error = None
    cfg = active_api()
    if query and cfg:
        try:
            raw = api_get(cfg, cfg["search_path"], {cfg["search_param"] or "query": query})
            api_results = normalize_api_result(cfg, raw)
            if kind in {"movie", "tv"}:
                api_results = [r for r in api_results if ("tv" in r["kind"] if kind == "tv" else "tv" not in r["kind"]) ]
        except Exception as exc:
            api_error = "Connected API search failed. Check API & Integrations in Admin."
            log_system_error(exc, source="api-search", error_type=type(exc).__name__, status=502)
    return render_template("browse.html", items=items, query=query, kind=kind, genre=genre, api_results=api_results, api_connected=bool(cfg), api_error=api_error)


@app.route("/title/<int:title_id>")
def details(title_id):
    item = title_row(title_id)
    episodes = db().execute("SELECT * FROM episodes WHERE title_id=? ORDER BY season, episode", (title_id,)).fetchall()
    similar = db().execute("SELECT * FROM titles WHERE id<>? AND kind=? ORDER BY rating DESC LIMIT 6", (title_id, item["kind"])).fetchall()
    return render_template("details.html", item=item, episodes=episodes, similar=similar)


@app.route("/watch/<int:title_id>")
def watch(title_id):
    item = title_row(title_id)
    if item["kind"] == "tv":
        ep_id = request.args.get("episode", type=int)
        if ep_id:
            ep = db().execute("SELECT * FROM episodes WHERE id=? AND title_id=?", (ep_id, title_id)).fetchone()
        else:
            ep = db().execute("SELECT * FROM episodes WHERE title_id=? ORDER BY season, episode LIMIT 1", (title_id,)).fetchone()
        if not ep:
            return render_template("player.html", item=item, episode=None, stream_url=None, error="No episode has been added yet."), 404
        return render_template("player.html", item=item, episode=ep, stream_url=media_url(ep), error=None)
    return render_template("player.html", item=item, episode=None, stream_url=media_url(item), error=None)


def safe_local_path(relative_name):
    if not relative_name:
        return None
    candidate = (MEDIA_DIR / relative_name).resolve()
    if MEDIA_DIR.resolve() not in candidate.parents and candidate != MEDIA_DIR.resolve():
        return None
    return candidate


def media_url(row):
    if row["source_type"] == "remote" and row["source_url"]:
        return row["source_url"]
    if row["local_file"]:
        return url_for("media_file", filename=row["local_file"])
    return ""


@app.route("/media/<path:filename>")
def media_file(filename):
    path = safe_local_path(filename)
    if not path or not path.exists() or not path.is_file():
        abort(404)
    return send_file(path, conditional=True)


@app.route("/download/<int:title_id>")
def download(title_id):
    item = title_row(title_id)
    if item["kind"] == "tv":
        ep_id = request.args.get("episode", type=int)
        if not ep_id:
            return redirect(url_for("details", title_id=title_id))
        row = db().execute("SELECT * FROM episodes WHERE id=? AND title_id=?", (ep_id, title_id)).fetchone()
        if not row:
            abort(404)
    else:
        row = item
    if row["source_type"] != "local" or not row["local_file"]:
        return render_template("download_info.html", item=item, row=row)
    path = safe_local_path(row["local_file"])
    if not path or not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


# ----------------------- Generic authorized API connector -----------------------

API_DEFAULTS = {
    "search_path": "/search",
    "info_path": "/info/{id}",
    "seasons_path": "/seasons/{id}",
    "episodes_path": "/episodes/{id}",
    "stream_path": "/stream/{id}",
    "download_path": "/dl/{id}",
    "subtitles_path": "/subtitles/{id}",
    "search_param": "query",
    "headers_json": "{}",
    "search_results_path": "results",
    "id_key": "id",
    "title_key": "title",
    "poster_key": "poster",
    "backdrop_key": "backdrop",
    "year_key": "year",
    "type_key": "type",
    "description_key": "description",
    "stream_url_key": "url",
}

def apply_api_defaults(form):
    out = {}
    for key, default in API_DEFAULTS.items():
        value = (form.get(key) or "").strip()
        out[key] = value or default
    return out

def active_api():
    return db().execute("SELECT * FROM api_configs WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()


def json_path(data, path):
    if not path:
        return data
    value = data
    for piece in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            value = value.get(piece)
        elif isinstance(value, list) and piece.isdigit() and int(piece) < len(value):
            value = value[int(piece)]
        else:
            return None
    return value


def first_value(data, keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def api_headers(cfg):
    raw = cfg["headers_json"] or "{}"
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def api_url(cfg, path, **values):
    base = (cfg["base_url"] or "").strip()
    path = (path or "").strip()
    for key, value in values.items():
        path = path.replace("{" + key + "}", quote(str(value), safe=""))
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def api_get(cfg, path, params=None):
    response = requests.get(
        api_url(cfg, path),
        params=params or {},
        headers=api_headers(cfg),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def normalize_api_result(cfg, raw):
    configured = json_path(raw, cfg["search_results_path"])
    results = configured
    if results is None:
        results = raw.get("results") if isinstance(raw, dict) else None
    if results is None and isinstance(raw, dict):
        for key in ("data", "items", "list", "movies", "shows", "results"):
            if isinstance(raw.get(key), list):
                results = raw[key]
                break
    if not isinstance(results, list):
        if isinstance(raw, list):
            results = raw
        else:
            results = []
    output = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = json_path(item, cfg["title_key"]) if cfg["title_key"] else first_value(item, ["title", "name", "original_title", "subjectName"])
        external_id = json_path(item, cfg["id_key"]) if cfg["id_key"] else first_value(item, ["id", "subjectId", "subject_id", "tmdb_id"])
        poster = json_path(item, cfg["poster_key"]) if cfg["poster_key"] else first_value(item, ["poster", "posterUrl", "poster_path", "image", "cover"])
        backdrop = json_path(item, cfg["backdrop_key"]) if cfg["backdrop_key"] else first_value(item, ["backdrop", "backdropUrl", "backdrop_path", "background"])
        year = json_path(item, cfg["year_key"]) if cfg["year_key"] else first_value(item, ["year", "releaseYear", "release_date"])
        kind = json_path(item, cfg["type_key"]) if cfg["type_key"] else first_value(item, ["type", "kind", "media_type"])
        description = json_path(item, cfg["description_key"]) if cfg["description_key"] else first_value(item, ["description", "overview", "plot", "synopsis"])
        if not title or external_id is None:
            continue
        kind_text = str(kind or "movie").lower()
        if kind_text in {"series", "show", "tvshow", "tv-series", "television"}:
            kind_text = "tv"
        else:
            kind_text = "tv" if "tv" in kind_text else "movie"
        output.append({
            "id": str(external_id),
            "title": str(title),
            "poster": poster or "/static/posters/blaze.svg",
            "backdrop": backdrop or poster or "/static/posters/blaze.svg",
            "year": str(year or ""),
            "kind": kind_text,
            "description": str(description or ""),
        })
    return output


def stream_value(cfg, raw):
    if cfg["stream_url_key"]:
        value = json_path(raw, cfg["stream_url_key"])
        if isinstance(value, dict):
            for key in ("url", "stream_url", "streamUrl", "src", "playUrl", "play_url", "file"):
                if value.get(key):
                    return value[key]
        if isinstance(value, list):
            for item in value:
                found = stream_value(cfg, item)
                if found:
                    return found
        if isinstance(value, str):
            return value
    candidates = ["url", "stream_url", "streamUrl", "playUrl", "play_url", "src", "videoUrl", "video_url", "file"]
    if isinstance(raw, dict):
        direct = first_value(raw, candidates)
        if direct:
            return direct
        for key in ("data", "result", "source", "sources", "stream", "streams"):
            child = raw.get(key)
            if isinstance(child, (dict, list)):
                found = stream_value(cfg, child)
                if found:
                    return found
    if isinstance(raw, list):
        for item in raw:
            found = stream_value(cfg, item)
            if found:
                return found
    return raw if isinstance(raw, str) else None


@app.route("/api-library")
def api_library():
    cfg = active_api()
    q = request.args.get("q", "").strip()
    results = []
    error = None
    if q and cfg:
        try:
            raw = api_get(cfg, cfg["search_path"], {cfg["search_param"] or "query": q})
            results = normalize_api_result(cfg, raw)
        except Exception as exc:
            error = f"API request failed: {exc}"
            log_system_error(exc, source="api-library", error_type=type(exc).__name__, status=502)
    elif q and not cfg:
        error = "No API is active. Add one in Admin → API & Integrations."
    return render_template("api_library.html", config=cfg, query=q, results=results, error=error)


@app.route("/api/external/<path:external_id>")
def api_external_info(external_id):
    cfg = active_api()
    if not cfg or not cfg["info_path"]:
        return jsonify({"error": "No active API info endpoint configured."}), 400
    try:
        raw = api_get(cfg, cfg["info_path"].replace("{id}", quote(external_id, safe="")), {})
        return jsonify({"data": raw})
    except Exception as exc:
        log_system_error(exc, source="api-info", error_type=type(exc).__name__, status=502)
        return jsonify({"error": str(exc), "request_id": getattr(g, "request_id", "")}), 502


@app.route("/api-external-watch/<path:external_id>")
def api_external_watch(external_id):
    cfg = active_api()
    if not cfg or not cfg["stream_path"]:
        return render_template("external_player.html", item={"title": "API title", "description": ""}, stream_url=None, error="No stream endpoint has been configured in Admin → API & Integrations."), 400
    try:
        path = cfg["stream_path"].replace("{id}", quote(external_id, safe=""))
        raw = api_get(cfg, path, {})
        stream = stream_value(cfg, raw)
        if not stream:
            raise ValueError("The configured API did not return a recognizable playable URL. Set Stream URL field/path in Admin.")
        display_title = request.args.get("title", external_id)
        return render_template("external_player.html", item={"title": display_title, "description": "External API playback"}, stream_url=stream, error=None)
    except Exception as exc:
        display_title = request.args.get("title", external_id)
        log_system_error(exc, source="api-playback", error_type=type(exc).__name__, status=502)
        return render_template("external_player.html", item={"title": display_title, "description": ""}, stream_url=None, error=f"API playback failed: {exc}. Request ID: {getattr(g, 'request_id', '')}"), 502


# ------------------------------ Admin ---------------------------------

def admin_key():
    return os.environ.get("ADMIN_KEY", "change-me")


def is_admin():
    return session.get("admin_authenticated") is True


def require_admin():
    if not is_admin():
        abort(401)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("key", "") == admin_key():
            session["admin_authenticated"] = True
            return redirect(url_for("admin"))
        return render_template("admin_login.html", error="Invalid admin key."), 401
    return render_template("admin_login.html", error=None)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("home"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    require_admin()
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            title = request.form.get("title", "Untitled").strip()
            kind = request.form.get("kind", "movie")
            year = request.form.get("year", type=int)
            genres = request.form.get("genres", "").strip()
            description = request.form.get("description", "").strip()
            source_type = request.form.get("source_type", "local")
            source_url = request.form.get("source_url", "").strip() if source_type == "remote" else ""
            local_file = ""
            poster = "/static/posters/series.svg" if kind == "tv" else "/static/posters/blaze.svg"
            upload = request.files.get("media")
            if upload and upload.filename:
                ext = Path(upload.filename).suffix.lower().lstrip(".")
                if ext not in ALLOWED_VIDEO:
                    return "Unsupported video type", 400
                safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload.filename).name)
                filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{safe_name}"
                target = MEDIA_DIR / filename
                upload.save(target)
                local_file = filename
                source_type = "local"
            conn.execute(
                """INSERT INTO titles(kind,title,year,duration,rating,genres,description,poster,backdrop,featured,source_type,source_url,local_file,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (kind, title, year, "", "", genres, description, poster, poster, 0, source_type, source_url, local_file, now_iso()),
            )
            conn.commit()
            return redirect(url_for("admin"))

        if action == "save_api":
            headers_text = request.form.get("headers_json", "{}").strip() or "{}"
            try:
                if not isinstance(json.loads(headers_text), dict):
                    raise ValueError("Headers/Auth must be a JSON object.")
            except (json.JSONDecodeError, ValueError) as exc:
                log_system_error(exc, source="admin-api", error_type=type(exc).__name__, status=400, severity="WARNING")
                return render_template("admin.html", items=conn.execute("SELECT * FROM titles ORDER BY id DESC").fetchall(), users=conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall(), apis=conn.execute("SELECT * FROM api_configs ORDER BY id DESC").fetchall(), admin_key=admin_key(), api_error=str(exc), errors=recent_errors(), **admin_counts()), 400
            config_id = request.form.get("config_id", type=int)
            if not request.form.get("base_url", "").strip():
                return render_template("admin.html", items=conn.execute("SELECT * FROM titles ORDER BY id DESC").fetchall(), users=conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall(), apis=conn.execute("SELECT * FROM api_configs ORDER BY id DESC").fetchall(), admin_key=admin_key(), api_error="Base URL is required.", errors=recent_errors(), **admin_counts()), 400
            defaults = apply_api_defaults(request.form)
            defaults["headers_json"] = headers_text
            values = (
                request.form.get("name", "My API").strip() or "My API",
                request.form.get("base_url", "").strip(),
                defaults["search_path"], defaults["info_path"], defaults["seasons_path"], defaults["episodes_path"],
                defaults["stream_path"], defaults["download_path"], defaults["subtitles_path"], defaults["search_param"], defaults["headers_json"],
                defaults["search_results_path"], defaults["id_key"], defaults["title_key"], defaults["poster_key"], defaults["backdrop_key"],
                defaults["year_key"], defaults["type_key"], defaults["description_key"], defaults["stream_url_key"],
            )
            active = 1 if request.form.get("active") == "1" else 0
            if active:
                conn.execute("UPDATE api_configs SET active=0")
            stamp = now_iso()
            if config_id:
                conn.execute(
                    """UPDATE api_configs SET name=?,base_url=?,search_path=?,info_path=?,seasons_path=?,episodes_path=?,stream_path=?,download_path=?,subtitles_path=?,search_param=?,headers_json=?,search_results_path=?,id_key=?,title_key=?,poster_key=?,backdrop_key=?,year_key=?,type_key=?,description_key=?,stream_url_key=?,active=?,updated_at=? WHERE id=?""",
                    values + (active, stamp, config_id),
                )
            else:
                conn.execute(
                    """INSERT INTO api_configs(name,base_url,search_path,info_path,seasons_path,episodes_path,stream_path,download_path,subtitles_path,search_param,headers_json,search_results_path,id_key,title_key,poster_key,backdrop_key,year_key,type_key,description_key,stream_url_key,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values + (active, stamp, stamp),
                )
            conn.commit()
            return redirect(url_for("admin"))

        if action == "clear_errors":
            conn.execute("DELETE FROM system_errors")
            conn.commit()
            return redirect(url_for("admin"))

        if action == "activate_api":
            api_id = request.form.get("api_id", type=int)
            conn.execute("UPDATE api_configs SET active=0")
            conn.execute("UPDATE api_configs SET active=1, updated_at=? WHERE id=?", (now_iso(), api_id))
            conn.commit()
            return redirect(url_for("admin"))

        if action == "delete_api":
            api_id = request.form.get("api_id", type=int)
            conn.execute("DELETE FROM api_configs WHERE id=?", (api_id,))
            conn.commit()
            return redirect(url_for("admin"))

    items = conn.execute("SELECT * FROM titles ORDER BY id DESC").fetchall()
    users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    apis = conn.execute("SELECT * FROM api_configs ORDER BY id DESC").fetchall()
    return render_template("admin.html", items=items, users=users, apis=apis, admin_key=admin_key(), api_error=None, errors=recent_errors(), **admin_counts())


def recent_errors(limit=100):
    conn = db()
    return conn.execute("SELECT * FROM system_errors ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def admin_counts():
    conn = db()
    return {
        "user_count": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "active_api_count": conn.execute("SELECT COUNT(*) FROM api_configs WHERE active=1").fetchone()[0],
        "error_count": conn.execute("SELECT COUNT(*) FROM system_errors WHERE severity='ERROR'").fetchone()[0],
    }


@app.route("/admin/delete/<int:title_id>", methods=["POST"])
def admin_delete(title_id):
    require_admin()
    conn = db()
    conn.execute("DELETE FROM episodes WHERE title_id=?", (title_id,))
    conn.execute("DELETE FROM titles WHERE id=?", (title_id,))
    conn.commit()
    return redirect(url_for("admin"))


@app.route("/admin/users.csv")
def users_csv():
    require_admin()
    rows = db().execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    lines = ["id,name,phone,latitude,longitude,accuracy_m,location_consent,joined_at,last_seen"]
    for row in rows:
        vals = [row[k] for k in ("id","name","phone","latitude","longitude","accuracy_m","location_consent","joined_at","last_seen")]
        lines.append(",".join('"' + str(v or "").replace('"', '""') + '"' for v in vals))
    from io import BytesIO
    return send_file(BytesIO(("\n".join(lines) + "\n").encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="tms_users.csv")


@app.context_processor
def globals_for_templates():
    return {"app_name": "TM & S", "year_now": datetime.now().year, "signed_in_user": session.get("user_id"), "admin_signed_in": is_admin()}


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    like = f"%{q}%"
    rows = db().execute(
        "SELECT * FROM titles WHERE title LIKE ? OR description LIKE ? OR genres LIKE ? ORDER BY featured DESC, rating DESC LIMIT 20",
        (like, like, like),
    ).fetchall()
    return jsonify([as_dict(r) for r in rows])


@app.route("/api/title/<int:title_id>")
def api_title(title_id):
    item = title_row(title_id)
    episodes = db().execute("SELECT * FROM episodes WHERE title_id=? ORDER BY season, episode", (title_id,)).fetchall()
    payload = as_dict(item)
    payload["episodes"] = [as_dict(x) for x in episodes]
    return jsonify(payload)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
