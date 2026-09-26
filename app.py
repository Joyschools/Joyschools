import json
import os
import re
import sqlite3
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        g.db = sqlite3.connect(DB_PATH, timeout=20)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=20000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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

        CREATE TABLE IF NOT EXISTS metadata_cache (
            cache_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
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


# Initialize the schema once when the worker imports the application.
# Render uses one Gunicorn worker in Procfile to keep SQLite predictable.
init_db()

@app.before_request
def bootstrap():
    # Database initialization happens once at startup, not on every request.
    # This avoids SQLite schema/write races when Render serves concurrent requests.
    g.request_id = request.headers.get("X-Request-ID") or os.urandom(8).hex()
    if session.get("user_id"):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("UPDATE users SET last_seen=? WHERE id=?", (now_iso(), session["user_id"]))
            conn.commit(); conn.close()
        except sqlite3.Error as exc:
            log_system_error(exc, source="session-last-seen", error_type=type(exc).__name__, status=503, severity="WARNING")


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
    if request.path == "/promise21232425" and not is_admin():
        return render_template("admin_login.html", error=f"TM & S encountered a system error. Request ID: {getattr(g, 'request_id', '')}"), 500
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
    popular = db().execute("SELECT * FROM titles ORDER BY featured DESC, rating DESC, id DESC LIMIT 8").fetchall()
    movies = db().execute("SELECT * FROM titles WHERE kind='movie' ORDER BY id DESC LIMIT 8").fetchall()
    tv = db().execute("SELECT * FROM titles WHERE kind='tv' ORDER BY id DESC LIMIT 8").fetchall()
    discover = get_home_discover()
    discover_movies = [x for x in discover if x["kind"]=="movie"]
    discover_tv = [x for x in discover if x["kind"]=="tv"]
    return render_template("home.html", featured=featured, popular=popular, movies=movies, tv=tv, discover=discover, discover_movies=discover_movies, discover_tv=discover_tv, api_connected=bool(active_api()))


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

    discover_results = []
    discover_error = None
    if query:
        try:
            kinds = [kind] if kind in {"movie", "tv"} else ["movie", "tv"]
            for discover_kind in kinds:
                discover_results.extend(cinemeta_search(query, discover_kind))
        except Exception as exc:
            discover_error = "Live movie/series discovery is temporarily unavailable."
            log_system_error(exc, source="metadata-search", error_type=type(exc).__name__, status=502)

    api_results = []
    api_error = None
    cfg = active_api()
    if query and cfg:
        try:
            raw = api_get(cfg, cfg["search_path"], {cfg["search_param"] or "query": query})
            api_results = normalize_api_result(cfg, raw)
            if kind in {"movie", "tv"}:
                api_results = [r for r in api_results if r["kind"] == kind]
        except Exception as exc:
            api_error = "Connected playback API search failed. The live discovery cards are still available."
            log_system_error(exc, source="playback-api-search", error_type=type(exc).__name__, status=502)
    return render_template("browse.html", items=items, query=query, kind=kind, genre=genre, discover_results=discover_results, api_results=api_results, api_connected=bool(cfg), api_error=api_error, discover_error=discover_error)


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


# ----------------------- Public metadata discovery -----------------------

CINEMETA_BASE = os.environ.get("CINEMETA_BASE", "https://v3-cinemeta.strem.io").rstrip("/")

def cinemeta_kind_path(kind):
    return "series" if kind == "tv" else kind


HOME_DISCOVERY = [
    ("movie", "tt1375666"),  # Inception
    ("movie", "tt0816692"),  # Interstellar
    ("movie", "tt0111161"),  # The Shawshank Redemption
    ("movie", "tt0468569"),  # The Dark Knight
    ("tv", "tt0903747"),     # Breaking Bad
    ("tv", "tt4574334"),     # Stranger Things
    ("tv", "tt0944947"),     # Game of Thrones
    ("tv", "tt3581920"),     # The Last of Us
]

def metadata_request(url, timeout=12):
    response = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
    response.raise_for_status()
    return response.json()

def parse_year(value):
    if not value:
        return ""
    text = str(value)
    match = re.search(r"(\d{4})", text)
    return match.group(1) if match else text[:4]

def normalize_cinemeta_meta(meta, fallback_kind="movie"):
    if not isinstance(meta, dict):
        return None
    item_id = meta.get("id")
    title = meta.get("name") or meta.get("title")
    if not item_id or not title:
        return None
    kind = meta.get("type") or fallback_kind
    kind = "tv" if str(kind).lower() in {"series", "tv", "show"} else "movie"
    poster = meta.get("poster") or ""
    backdrop = meta.get("background") or meta.get("backdrop") or poster
    return {
        "id": str(item_id),
        "title": str(title),
        "poster": poster or ("/static/posters/series.svg" if kind == "tv" else "/static/posters/blaze.svg"),
        "backdrop": backdrop or poster,
        "year": parse_year(meta.get("releaseInfo") or meta.get("year") or meta.get("releaseDate")),
        "kind": kind,
        "description": str(meta.get("description") or meta.get("overview") or ""),
        "rating": str(meta.get("imdbRating") or ""),
        "genre": ", ".join(meta.get("genre", [])) if isinstance(meta.get("genre"), list) else str(meta.get("genre") or ""),
    }

def cinemeta_search(query, kind):
    encoded = quote(query, safe="")
    data = metadata_request(f"{CINEMETA_BASE}/catalog/{cinemeta_kind_path(kind)}/top/search={encoded}.json")
    metas = data.get("metas") if isinstance(data, dict) else []
    return [x for x in (normalize_cinemeta_meta(m, kind) for m in metas or []) if x]

def cinemeta_meta(kind, external_id):
    key = f"meta:{kind}:{external_id}"
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        cached = conn.execute("SELECT payload_json FROM metadata_cache WHERE cache_key=?", (key,)).fetchone()
        if cached:
            try:
                return json.loads(cached["payload_json"])
            except Exception:
                pass
        data = metadata_request(f"{CINEMETA_BASE}/meta/{cinemeta_kind_path(kind)}/{quote(str(external_id), safe=":")}.json")
        meta = normalize_cinemeta_meta(data.get("meta") if isinstance(data, dict) else None, kind)
        if not meta:
            raise ValueError("No metadata was returned for that title.")
        conn.execute("INSERT OR REPLACE INTO metadata_cache(cache_key,payload_json,updated_at) VALUES(?,?,?)", (key, json.dumps(meta), now_iso()))
        conn.commit()
        return meta
    finally:
        conn.close()

def get_cinemeta_catalog(kind, catalog="top", limit=24):
    key=f"catalog:{kind}:{catalog}:{limit}"
    conn=sqlite3.connect(DB_PATH, timeout=10); conn.row_factory=sqlite3.Row
    try:
        cached=conn.execute("SELECT payload_json FROM metadata_cache WHERE cache_key=?",(key,)).fetchone()
        if cached:
            try: return json.loads(cached["payload_json"])
            except Exception: pass
        data=metadata_request(f"{CINEMETA_BASE}/catalog/{kind}/{catalog}.json")
        metas=data.get("metas") if isinstance(data,dict) else []
        out=[x for x in (normalize_cinemeta_meta(m,kind) for m in metas or []) if x][:limit]
        conn.execute("INSERT OR REPLACE INTO metadata_cache(cache_key,payload_json,updated_at) VALUES(?,?,?)",(key,json.dumps(out),now_iso())); conn.commit()
        return out
    finally: conn.close()

def get_home_discover():
    output=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(get_cinemeta_catalog,"movie","top",24),pool.submit(get_cinemeta_catalog,"series","top",24)]
        for future in futures:
            try: output.extend(future.result())
            except Exception as exc: log_system_error(exc,source="metadata-home",error_type=type(exc).__name__,status=502,severity="WARNING")
    # Keep a varied, deterministic mix and remove duplicate IDs.
    seen=set(); unique=[]
    for item in output:
        key=(item["kind"],item["id"])
        if key not in seen:
            seen.add(key); unique.append(item)
    return unique[:48]


# ----------------------- Generic authorized API connector -----------------------

API_DEFAULTS = {
    # These defaults also match the MovieBox wrapper the user can enter manually.
    "search_path": "/search",
    "info_path": "/info/{id}",
    "seasons_path": "/seasons/{id}",
    "episodes_path": "/episodes/{id}/{se}",
    "stream_path": "/sources/{id}/{se}/{ep}",
    "download_path": "/download/{id}/{se}",
    "subtitles_path": "/captions/ext/{id}/{resourceId}/{ep}",
    "search_param": "q",
    "headers_json": "{}",
    "search_results_path": "results",
    "id_key": "subjectId",
    "title_key": "title",
    "poster_key": "poster",
    "backdrop_key": "backdrop",
    "year_key": "releaseDate",
    "type_key": "subjectType",
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
        encoded = quote(str(value), safe="")
        path = path.replace("{" + key + "}", encoded)
        path = path.replace(":" + key, encoded)
    # Common provider spelling used for subject IDs.
    if "id" in values:
        encoded_id = quote(str(values["id"]), safe="")
        path = path.replace(":subjectId", encoded_id)
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


def normalize_kind(value):
    text = str(value or "").strip().lower()
    if text in {"2", "tv", "series", "show", "tvshow", "tv-series", "television"}:
        return "tv"
    if text in {"1", "movie", "film", "feature"}:
        return "movie"
    if text in {"7", "shorts", "short"}:
        return "tv"
    return "tv" if "tv" in text or "series" in text else "movie"

def normalize_api_result(cfg, raw):
    configured = json_path(raw, cfg["search_results_path"])
    results = configured
    if results is None:
        results = raw.get("results") if isinstance(raw, dict) else None
    if results is None and isinstance(raw, dict):
        for key in ("data", "items", "list", "movies", "shows", "series", "subjects", "results"):
            if isinstance(raw.get(key), list):
                results = raw[key]
                break
        if results is None:
            for parent in ("data", "result", "response"):
                child = raw.get(parent)
                if isinstance(child, dict):
                    for key in ("items", "movies", "shows", "series", "subjects", "results"):
                        if isinstance(child.get(key), list):
                            results = child[key]
                            break
                if results is not None:
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
        poster = json_path(item, cfg["poster_key"]) if cfg["poster_key"] else first_value(item, ["poster", "posterUrl", "poster_path", "image", "cover", "coverUrl", "imageUrl"])
        backdrop = json_path(item, cfg["backdrop_key"]) if cfg["backdrop_key"] else first_value(item, ["backdrop", "backdropUrl", "backdrop_path", "background", "backgroundUrl", "backdrop_url"])
        year = json_path(item, cfg["year_key"]) if cfg["year_key"] else first_value(item, ["year", "releaseYear", "release_date"])
        kind = json_path(item, cfg["type_key"]) if cfg["type_key"] else first_value(item, ["type", "kind", "media_type"])
        description = json_path(item, cfg["description_key"]) if cfg["description_key"] else first_value(item, ["description", "overview", "plot", "synopsis"])
        if not title or external_id is None:
            continue
        kind_text = normalize_kind(kind)
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
    candidates = ["url", "stream_url", "streamUrl", "playUrl", "play_url", "src", "videoUrl", "video_url", "file", "streamURL", "playbackUrl", "playback_url"]
    if isinstance(raw, dict):
        direct = first_value(raw, candidates)
        if direct:
            return direct
        for key in ("data", "result", "source", "sources", "stream", "streams", "resource", "resources", "playInfo", "play_info"):
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
    require_admin()
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


def extract_provider_episodes(raw, default_season=None):
    seasons = []
    if isinstance(raw, dict):
        for key in ("seasons", "data", "results", "items", "episodes"):
            value = raw.get(key)
            if isinstance(value, list):
                if key == "episodes":
                    value = [{"season": default_season or 1, "episodes": value}]
                seasons.extend(value)
            elif isinstance(value, dict) and key == "data":
                for k in ("seasons", "episodes", "items", "results"):
                    v=value.get(k)
                    if isinstance(v,list):
                        if k=="episodes": v=[{"season": default_season or 1, "episodes": v}]
                        seasons.extend(v)
    elif isinstance(raw, list):
        seasons = [{"season": default_season or 1, "episodes": raw}]
    out=[]
    for group in seasons:
        if not isinstance(group, dict):
            continue
        season = group.get("season") or group.get("seasonNumber") or default_season or 1
        try: season=int(season)
        except Exception: season=1
        eps = group.get("episodes") or group.get("items") or group.get("results") or []
        if isinstance(eps, dict): eps=list(eps.values())
        if isinstance(eps, list) and eps and all(isinstance(x, dict) for x in eps):
            for e in eps:
                num=e.get("episode") or e.get("episodeNumber") or e.get("ep") or e.get("number")
                if num is None: continue
                try: num=int(num)
                except Exception: continue
                out.append({"season":season,"episode":num,"name":str(e.get("title") or e.get("name") or e.get("episodeTitle") or f"Episode {num}")})
    unique={(x["season"],x["episode"]):x for x in out}
    return sorted(unique.values(), key=lambda x:(x["season"],x["episode"]))

def match_provider_title(query_title, provider_results, year=""):
    if not provider_results:
        return None
    target = re.sub(r"[^a-z0-9]+", " ", query_title.casefold()).strip()
    target_year = parse_year(year)
    best=None; best_score=0.0
    for candidate in provider_results:
        cand = re.sub(r"[^a-z0-9]+", " ", candidate.get("title","").casefold()).strip()
        score=SequenceMatcher(None,target,cand).ratio()
        if target and cand == target: score += 0.5
        if target_year and parse_year(candidate.get("year")) == target_year: score += 0.1
        if score > best_score:
            best_score=score; best=candidate
    return best if best_score >= 0.62 else None

@app.route("/external/<kind>/<path:external_id>")
def external_details(kind, external_id):
    if kind not in {"movie", "tv"}:
        abort(404)
    try:
        data = metadata_request(f"{CINEMETA_BASE}/meta/{cinemeta_kind_path(kind)}/{quote(str(external_id), safe=":")}.json")
        raw_meta = data.get("meta") if isinstance(data, dict) else None
        item = normalize_cinemeta_meta(raw_meta, kind)
        if not item:
            raise ValueError("No metadata was returned for that title.")
        # Keep the normalized card cached so future home/detail views are quick.
        db().execute("INSERT OR REPLACE INTO metadata_cache(cache_key,payload_json,updated_at) VALUES(?,?,?)", (f"meta:{kind}:{external_id}", json.dumps(item), now_iso()))
        db().commit()
        cfg = active_api()
        provider_info = None
        info_error = None
        episodes = []
        provider_id = request.args.get("provider_id", "").strip()
        provider_title = ""
        if cfg and not provider_id and cfg["search_path"]:
            try:
                provider_raw = api_get(cfg, cfg["search_path"], {cfg["search_param"] or "q": item["title"]})
                provider_matches = [m for m in normalize_api_result(cfg, provider_raw) if m["kind"] == kind]
                chosen = match_provider_title(item["title"], provider_matches, item.get("year", ""))
                if chosen:
                    provider_id = chosen["id"]
                    provider_title = chosen["title"]
            except Exception as exc:
                info_error = "Playback source could not match this title yet."
                log_system_error(exc, source="playback-api-match", error_type=type(exc).__name__, status=502, severity="WARNING")
        if kind == "tv":
            # Prefer the playback provider's actual seasons/episodes so the selected
            # provider ID and episode numbering stay in sync. Fall back to metadata.
            if cfg and provider_id and cfg["seasons_path"]:
                try:
                    season_raw = api_get(cfg, cfg["seasons_path"], {}) if "{id}" not in cfg["seasons_path"] else api_get(cfg, cfg["seasons_path"], {})
                    episodes = extract_provider_episodes(season_raw)
                except Exception as exc:
                    log_system_error(exc, source="playback-api-seasons", error_type=type(exc).__name__, status=502, severity="WARNING")
            if not episodes and cfg and provider_id and cfg["episodes_path"]:
                for season_no in range(1, 13):
                    try:
                        path = api_url(cfg, cfg["episodes_path"], id=provider_id, external_id=provider_id, se=season_no, season=season_no)
                        raw_eps = requests.get(path, headers=api_headers(cfg), params={"page":1,"perPage":50}, timeout=12)
                        if raw_eps.status_code == 404:
                            if season_no > 1: break
                            continue
                        raw_eps.raise_for_status()
                        found = extract_provider_episodes(raw_eps.json(), default_season=season_no)
                        episodes.extend(found)
                        if not found and season_no > 1: break
                    except Exception:
                        if season_no > 1: break
            if not episodes and isinstance(raw_meta, dict):
                for video in raw_meta.get("videos") or []:
                    if not isinstance(video, dict):
                        continue
                    season = video.get("season")
                    episode = video.get("episode")
                    if season is None or episode is None or int(season) < 1:
                        continue
                    episodes.append({
                        "season": int(season),
                        "episode": int(episode),
                        "name": str(video.get("name") or video.get("title") or f"Episode {episode}"),
                    })
                episodes.sort(key=lambda e: (e["season"], e["episode"]))
        if cfg and cfg["info_path"] and provider_id:
            try:
                info_path = api_url(cfg, cfg["info_path"], id=provider_id, external_id=provider_id)
                response = requests.get(info_path, headers=api_headers(cfg), timeout=15)
                response.raise_for_status()
                provider_info = response.json()
            except Exception as exc:
                info_error = "Playback provider details could not be loaded; you can still use the metadata card."
                log_system_error(exc, source="playback-api-info", error_type=type(exc).__name__, status=502, severity="WARNING")
        return render_template("external_details.html", item=item, cfg=cfg, provider_info=provider_info, info_error=info_error, episodes=episodes, external_id=external_id, provider_id=provider_id, provider_title=provider_title)
    except Exception as exc:
        log_system_error(exc, source="metadata-detail", error_type=type(exc).__name__, status=502)
        fallback = {"title":"Title unavailable","description":"","poster":"/static/posters/blaze.svg","backdrop":"/static/posters/blaze.svg","kind":kind,"year":"", "rating":""}
        return render_template("external_details.html", item=fallback, cfg=active_api(), provider_info=None, info_error="This title could not be loaded right now. Check Admin → System errors.", episodes=[], external_id=external_id, provider_id=request.args.get("provider_id", ""), provider_title=""), 502

@app.route("/api/external/<path:external_id>")
def api_external_info(external_id):
    cfg = active_api()
    if not cfg or not cfg["info_path"]:
        return jsonify({"error": "No active playback API info endpoint configured."}), 400
    try:
        raw = api_get(cfg, cfg["info_path"].replace("{id}", quote(external_id, safe="")), {})
        return jsonify({"data": raw})
    except Exception as exc:
        log_system_error(exc, source="playback-api-info", error_type=type(exc).__name__, status=502)
        return jsonify({"error": str(exc), "request_id": getattr(g, "request_id", "")}), 502

@app.route("/external-watch/<kind>/<path:external_id>")
def external_watch(kind, external_id):
    cfg = active_api()
    if not cfg or not cfg["stream_path"]:
        return render_template("external_player.html", item={"title": request.args.get("title", "TM & S title"), "description": ""}, stream_url=None, error="No playback API is active. Add one in the Admin area first."), 400
    season = request.args.get("season", "")
    episode = request.args.get("episode", "")
    try:
        playback_id = request.args.get("provider_id", "").strip() or external_id
        path = api_url(cfg, cfg["stream_path"], id=playback_id, external_id=playback_id, season=season, episode=episode)
        response = requests.get(path, headers=api_headers(cfg), timeout=15)
        response.raise_for_status()
        raw = response.json()
        stream = stream_value(cfg, raw)
        if not stream:
            raise ValueError("The configured playback API did not return a recognizable playable URL. Review Stream URL mapping in Admin.")
        display_title = request.args.get("title", external_id)
        return render_template("external_player.html", item={"title": display_title, "description": "Playback from the configured provider."}, stream_url=stream, error=None)
    except Exception as exc:
        display_title = request.args.get("title", external_id)
        log_system_error(exc, source="playback-api-stream", error_type=type(exc).__name__, status=502)
        return render_template("external_player.html", item={"title": display_title, "description": ""}, stream_url=None, error=f"Playback failed: {exc}. Request ID: {getattr(g, 'request_id', '')}"), 502

# Backward-compatible internal endpoint used by older templates; it now delegates to the same connector.
@app.route("/api-external-watch/<path:external_id>")
def api_external_watch_legacy(external_id):
    return redirect(url_for("external_watch", kind=request.args.get("kind", "movie"), external_id=external_id, title=request.args.get("title", external_id), season=request.args.get("season", ""), episode=request.args.get("episode", "")))


# ------------------------------ Admin ---------------------------------

def admin_name():
    return os.environ.get("ADMIN_NAME", "admin").strip()


def is_admin():
    return session.get("admin_authenticated") is True


def require_admin():
    if not is_admin():
        abort(404)


@app.route("/promise21232425", methods=["GET", "POST"])
def admin():
    # One doorway: GET shows the single admin-name box; authenticated POST handles dashboard actions.
    if not is_admin():
        if request.method == "POST":
            entered = request.form.get("admin_name", "").strip()
            if entered and entered == admin_name():
                session["admin_authenticated"] = True
                return redirect(url_for("admin"))
            return render_template("admin_login.html", error="That admin name does not match the name configured on Render."), 401
        return render_template("admin_login.html", error=None)
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
                return render_template("admin.html", items=conn.execute("SELECT * FROM titles ORDER BY id DESC").fetchall(), users=conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall(), apis=conn.execute("SELECT * FROM api_configs ORDER BY id DESC").fetchall(), admin_name=admin_name(), api_error=str(exc), errors=recent_errors(), **admin_counts()), 400
            config_id = request.form.get("config_id", type=int)
            if not request.form.get("base_url", "").strip():
                return render_template("admin.html", items=conn.execute("SELECT * FROM titles ORDER BY id DESC").fetchall(), users=conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall(), apis=conn.execute("SELECT * FROM api_configs ORDER BY id DESC").fetchall(), admin_name=admin_name(), api_error="Base URL is required.", errors=recent_errors(), **admin_counts()), 400
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
    return render_template("admin.html", items=items, users=users, apis=apis, admin_name=admin_name(), api_error=None, errors=recent_errors(), **admin_counts())


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


@app.route("/promise21232425/delete/<int:title_id>", methods=["POST"])
def admin_delete(title_id):
    require_admin()
    conn = db()
    conn.execute("DELETE FROM episodes WHERE title_id=?", (title_id,))
    conn.execute("DELETE FROM titles WHERE id=?", (title_id,))
    conn.commit()
    return redirect(url_for("admin"))


@app.route("/promise21232425/users.csv")
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


@app.route("/api/client-error", methods=["POST"])
def api_client_error():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "Client-side error")
    source = str(payload.get("source") or "browser")
    error_type = str(payload.get("error_type") or "ClientError")
    route = str(payload.get("route") or request.referrer or request.path)
    log_system_error(message, source=source, error_type=error_type, status=400, severity="ERROR")
    return jsonify({"ok": True, "request_id": getattr(g, "request_id", "")})


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
