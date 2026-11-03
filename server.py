"""Local GEO brand diagnostic server. Run: python server.py"""

import csv
import base64
import binascii
import io
import json
import os
import re
import secrets
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, Response, g, jsonify, request, send_from_directory

from metrics import build_analytics
from browser_collect import PLATFORMS as BROWSER_PLATFORMS
from browser_collect import BrowserCollectionError, browser_ready, guest_remote_ready, collect_remote, list_guest_accounts
from browser_runtime import is_local
from login_center import login_blueprint
from app_paths import data_dir


ROOT = Path(__file__).resolve().parent
PLATFORMS = ("DP", "豆包", "Kimi", "千问", "文心一言", "元宝", "其他")
CSV_COLUMNS = ("question", "question_type", "platform", "observed_at", "asked_at", "status",
               "answer", "evidence_url", "page_url", "evidence_image", "error_message",
               "brand_mentioned", "brand_rank", "sentiment",
               "competitors", "citations", "positive_terms", "negative_terms", "rank_reviewed")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_text(value, *, label, maximum=200, required=False):
    text = str(value or "").strip()
    if required and not text:
        raise ApiError(f"{label}不能为空")
    if len(text) > maximum:
        raise ApiError(f"{label}不能超过 {maximum} 字")
    return text


def _names(value):
    if isinstance(value, str):
        value = value.replace("，", ",").replace("\n", ",").split(",")
    if not isinstance(value, list):
        raise ApiError("别名和竞品应为名称列表")
    names = []
    for item in value:
        name = _clean_text(item, label="名称", maximum=100)
        if name and name not in names:
            names.append(name)
    if len(names) > 30:
        raise ApiError("名称列表最多 30 项")
    return names


def _date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ApiError("日期需使用 YYYY-MM-DD 格式") from None


def _timestamp(value):
    value = _clean_text(value, label="提问时间", maximum=40)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("提问时间需为带时区的 ISO 时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError("提问时间需包含时区")
    return parsed.isoformat(timespec="seconds")


def _http_url(value, label):
    value = _clean_text(value, label=label, maximum=2000)
    if value and urlparse(value).scheme not in ("http", "https"):
        raise ApiError(f"{label}须以 http 或 https 开头")
    return value


def _json_list(value, label, maximum=50):
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    if not isinstance(value, list) or len(value) > maximum:
        raise ApiError(f"{label}需为不超过 {maximum} 项的列表")
    return value


def _rank(value):
    if value in (None, "", 0, "0"):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ApiError("排名需为正整数") from None
    if not 1 <= result <= 100:
        raise ApiError("排名需在 1 到 100 之间")
    return result


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "是", "提及")


def default_questions(brand, category, region):
    if not category:
        return [
            ("brand", f"{brand}怎么样？"),
            ("brand", f"{brand}值得推荐吗？"),
            ("brand", f"{brand}的口碑和主要争议是什么？"),
            ("brand", f"{brand}与同类品牌相比有什么优缺点？"),
        ]
    field = f"{region}{category}".strip() or "相关行业"
    return [
        ("keyword", f"{field}有哪些值得推荐的品牌或服务商？"),
        ("keyword", f"{field}怎么选择靠谱的服务商？"),
        ("keyword", f"{field}有哪些口碑较好的公司？"),
        ("keyword", f"{field}常见服务商对比与优缺点是什么？"),
        ("keyword", f"{field}性价比高的选择有哪些？"),
        ("brand", f"{brand}怎么样？"),
        ("brand", f"{brand}值得推荐吗？"),
        ("brand", f"{brand}的口碑和主要争议是什么？"),
        ("brand", f"{brand}与同类品牌相比有什么优缺点？"),
    ]


def create_app(test_config=None):
    app = Flask(__name__, static_folder="static")
    app.config.update(DATABASE=os.getenv("GEO_DB_PATH", str(data_dir() / "geo.sqlite3")),
                      EVIDENCE_DIR=str(data_dir() / "evidence"),
                      SEED_DEMO=True, MAX_CONTENT_LENGTH=5 * 1024 * 1024)
    if test_config:
        app.config.update(test_config)
    app.register_blueprint(login_blueprint())

    def db():
        if "db" not in g:
            path = Path(app.config["DATABASE"])
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            g.db = connection
        return g.db

    @app.teardown_appcontext
    def close_db(_exception):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def initialize():
        with app.app_context():
            conn = db()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY,
                    brand TEXT NOT NULL, category TEXT NOT NULL, region TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    competitors_json TEXT NOT NULL DEFAULT '[]',
                    is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                    text TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('keyword','brand')),
                    created_at TEXT NOT NULL, UNIQUE(project_id, text)
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                    question_id INTEGER NOT NULL REFERENCES questions(id),
                    platform TEXT NOT NULL, observed_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('complete','offline','error')),
                    answer TEXT NOT NULL, brand_mentioned INTEGER NOT NULL DEFAULT 0,
                    brand_rank INTEGER, sentiment TEXT NOT NULL DEFAULT 'unknown',
                    competitor_ranks_json TEXT NOT NULL DEFAULT '{}',
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    positive_terms_json TEXT NOT NULL DEFAULT '[]',
                    negative_terms_json TEXT NOT NULL DEFAULT '[]',
                    evidence_url TEXT NOT NULL DEFAULT '',
                    asked_at TEXT NOT NULL DEFAULT '',
                    page_url TEXT NOT NULL DEFAULT '',
                    evidence_image TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    source_kind TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observations_filter
                    ON observations(project_id, observed_at, platform, question_id);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(observations)")}
            if "evidence_url" not in columns:
                conn.execute("ALTER TABLE observations ADD COLUMN evidence_url TEXT NOT NULL DEFAULT ''")
            for name in ("asked_at", "page_url", "evidence_image", "error_message"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE observations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if "rank_reviewed" not in columns:
                conn.execute("ALTER TABLE observations ADD COLUMN rank_reviewed INTEGER NOT NULL DEFAULT 0")
            if app.config["SEED_DEMO"] and not conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                seed_demo(conn)
            conn.commit()

    def project_row(project_id):
        row = db().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise ApiError("项目不存在", 404)
        return project_obj(row)

    def require_real_project(project_id):
        project = project_row(project_id)
        if project["is_demo"]:
            raise ApiError("虚构演示项目只供查看；请创建新的品牌项目录入真实回答", 409)
        return project

    def project_obj(row):
        return {"id": row["id"], "brand": row["brand"], "category": row["category"],
                "region": row["region"], "aliases": json.loads(row["aliases_json"]),
                "competitors": json.loads(row["competitors_json"]),
                "is_demo": bool(row["is_demo"]), "created_at": row["created_at"]}

    def question_obj(row):
        return {"id": row["id"], "project_id": row["project_id"], "text": row["text"],
                "kind": row["kind"], "created_at": row["created_at"]}

    def observation_obj(row):
        return {"id": row["id"], "project_id": row["project_id"],
                "question_id": row["question_id"], "platform": row["platform"],
                "observed_at": row["observed_at"], "status": row["status"],
                "answer": row["answer"], "brand_mentioned": bool(row["brand_mentioned"]),
                "brand_rank": row["brand_rank"], "sentiment": row["sentiment"],
                "rank_reviewed": bool(row["rank_reviewed"] or row["brand_rank"] is not None),
                "competitor_ranks": json.loads(row["competitor_ranks_json"]),
                "citations": json.loads(row["citations_json"]),
                "positive_terms": json.loads(row["positive_terms_json"]),
                "negative_terms": json.loads(row["negative_terms_json"]),
                "evidence_url": row["evidence_url"],
                "asked_at": row["asked_at"], "page_url": row["page_url"],
                "evidence_image": row["evidence_image"], "error_message": row["error_message"],
                "source_kind": row["source_kind"], "created_at": row["created_at"]}

    def questions_for(project_id):
        return [question_obj(row) for row in db().execute(
            "SELECT * FROM questions WHERE project_id=? ORDER BY kind, id", (project_id,))]

    def observations_for(project_id):
        return [observation_obj(row) for row in db().execute(
            "SELECT * FROM observations WHERE project_id=?", (project_id,))]

    def normalize_observation(payload, project_id):
        project = project_row(project_id)
        try:
            question_id = int(payload.get("question_id"))
        except (TypeError, ValueError):
            raise ApiError("请选择问题句") from None
        if not db().execute("SELECT 1 FROM questions WHERE id=? AND project_id=?",
                            (question_id, project_id)).fetchone():
            raise ApiError("问题句不属于当前项目")
        platform = _clean_text(payload.get("platform"), label="平台", maximum=40, required=True)
        observed_at = _date(payload.get("observed_at") or date.today().isoformat())
        status = str(payload.get("status") or "complete").strip()
        if status not in ("complete", "offline", "error"):
            raise ApiError("状态只能是 complete、offline 或 error")
        answer = _clean_text(payload.get("answer"), label="模型回答", maximum=30000)
        if status == "complete" and not answer:
            raise ApiError("完成状态需要模型回答")
        evidence_url = _http_url(payload.get("evidence_url"), "模型会话链接")
        page_url = _http_url(payload.get("page_url"), "采集页面")
        asked_at = _timestamp(payload.get("asked_at"))
        evidence_image = _clean_text(payload.get("evidence_image"), label="截图文件", maximum=200)
        if evidence_image and not re.fullmatch(r"[a-zA-Z0-9_-]+\.png", evidence_image):
            raise ApiError("截图文件名无效")
        error_message = _clean_text(payload.get("error_message"), label="失败原因", maximum=500)
        rank = _rank(payload.get("brand_rank"))
        sentiment = str(payload.get("sentiment") or "unknown").strip()
        if sentiment not in ("positive", "neutral", "negative", "unknown"):
            raise ApiError("情绪应为正面、中性、负面或未标注")
        rivals = payload.get("competitor_ranks") or {}
        if isinstance(rivals, str):
            try:
                rivals = json.loads(rivals)
            except json.JSONDecodeError:
                raise ApiError("竞品排名需为 JSON 对象") from None
        if not isinstance(rivals, dict) or len(rivals) > 30:
            raise ApiError("竞品排名需为不超过 30 项的对象")
        rivals = {_clean_text(k, label="竞品名称", maximum=100, required=True): _rank(v)
                  for k, v in rivals.items()}
        citations = _json_list(payload.get("citations") or [], "引用")
        clean_citations = []
        for item in citations:
            if not isinstance(item, dict):
                raise ApiError("每条引用需包含标题、来源和链接")
            url = _clean_text(item.get("url"), label="引用链接", maximum=2000)
            if url and urlparse(url).scheme not in ("http", "https"):
                raise ApiError("引用链接须以 http 或 https 开头")
            clean_citations.append({"title": _clean_text(item.get("title"), label="引用标题", maximum=300),
                                    "source": _clean_text(item.get("source"), label="引用来源", maximum=150),
                                    "url": url})
        positive_terms = [_clean_text(t, label="正面词", maximum=80) for t in _json_list(payload.get("positive_terms") or [], "正面词")]
        negative_terms = [_clean_text(t, label="负面词", maximum=80) for t in _json_list(payload.get("negative_terms") or [], "负面词")]
        if "brand_mentioned" in payload:
            mentioned = _bool(payload["brand_mentioned"])
        else:
            mentioned = any(name.casefold() in answer.casefold()
                            for name in [project["brand"], *project["aliases"]] if name)
        mentioned = mentioned or rank is not None
        return {"question_id": question_id, "platform": platform, "observed_at": observed_at,
                "asked_at": asked_at, "status": status, "answer": answer,
                "evidence_url": evidence_url, "page_url": page_url,
                "evidence_image": evidence_image, "error_message": error_message,
                "brand_mentioned": mentioned,
                "brand_rank": rank, "sentiment": sentiment, "competitor_ranks": rivals,
                "rank_reviewed": _bool(payload.get("rank_reviewed", False)) or rank is not None,
                "citations": clean_citations, "positive_terms": [t for t in positive_terms if t],
                "negative_terms": [t for t in negative_terms if t]}

    def insert_observation(conn, project_id, data, source_kind="manual"):
        cursor = conn.execute("""INSERT INTO observations
            (project_id, question_id, platform, observed_at, status, answer,
             brand_mentioned, brand_rank, sentiment, competitor_ranks_json,
             citations_json, positive_terms_json, negative_terms_json, evidence_url,
             asked_at, page_url, evidence_image, error_message, source_kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, data["question_id"], data["platform"], data["observed_at"],
             data["status"], data["answer"], int(data["brand_mentioned"]), data["brand_rank"],
             data["sentiment"], json.dumps(data["competitor_ranks"], ensure_ascii=False),
             json.dumps(data["citations"], ensure_ascii=False),
             json.dumps(data["positive_terms"], ensure_ascii=False),
             json.dumps(data["negative_terms"], ensure_ascii=False),
             data["evidence_url"], data["asked_at"], data["page_url"],
             data["evidence_image"], data["error_message"], source_kind, _now()))
        conn.execute("UPDATE observations SET rank_reviewed=? WHERE id=?",
                     (int(data["rank_reviewed"]), cursor.lastrowid))
        return cursor.lastrowid

    @app.errorhandler(ApiError)
    def api_error(exc):
        return jsonify({"error": str(exc)}), exc.status

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify({"error": "文件不能超过 5 MB"}), 413

    @app.get("/")
    @app.get("/report/<int:_project_id>")
    def index(_project_id=None):
        html = (Path(app.static_folder) / "index.html").read_text(encoding="utf-8")
        return Response(html.replace("__DESKTOP_TOKEN__", app.config.get("DESKTOP_TOKEN", "")),
                        mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/api/projects")
    def list_projects():
        rows = db().execute("""SELECT p.*, COUNT(o.id) AS observation_count
            FROM projects p LEFT JOIN observations o ON o.project_id=p.id
            GROUP BY p.id ORDER BY p.is_demo, p.id DESC""").fetchall()
        return jsonify([{**project_obj(row), "observation_count": row["observation_count"]} for row in rows])

    @app.post("/api/projects")
    def add_project():
        payload = request.get_json(silent=True) or {}
        brand = _clean_text(payload.get("brand"), label="品牌", maximum=120, required=True)
        category = _clean_text(payload.get("category"), label="品类", maximum=120)
        region = _clean_text(payload.get("region"), label="地区", maximum=120)
        aliases = _names(payload.get("aliases") or [])
        competitors = [name for name in _names(payload.get("competitors") or []) if name != brand]
        conn = db()
        cursor = conn.execute("""INSERT INTO projects
            (brand,category,region,aliases_json,competitors_json,is_demo,created_at)
            VALUES (?,?,?,?,?,0,?)""",
            (brand, category, region, json.dumps(aliases, ensure_ascii=False),
             json.dumps(competitors, ensure_ascii=False), _now()))
        for kind, text in default_questions(brand, category, region):
            conn.execute("INSERT INTO questions (project_id,text,kind,created_at) VALUES (?,?,?,?)",
                         (cursor.lastrowid, text, kind, _now()))
        conn.commit()
        return jsonify(project_row(cursor.lastrowid)), 201

    @app.get("/api/projects/<int:project_id>")
    def get_project(project_id):
        return jsonify({**project_row(project_id), "questions": questions_for(project_id)})

    @app.put("/api/projects/<int:project_id>")
    def update_project(project_id):
        current = require_real_project(project_id)
        payload = request.get_json(silent=True) or {}
        brand = _clean_text(payload.get("brand", current["brand"]), label="品牌", maximum=120, required=True)
        category = _clean_text(payload.get("category", current["category"]), label="品类", maximum=120)
        region = _clean_text(payload.get("region", current["region"]), label="地区", maximum=120)
        aliases = _names(payload.get("aliases", current["aliases"]))
        competitors = [name for name in _names(payload.get("competitors", current["competitors"])) if name != brand]
        db().execute("""UPDATE projects SET brand=?,category=?,region=?,aliases_json=?,competitors_json=?
            WHERE id=?""", (brand, category, region, json.dumps(aliases, ensure_ascii=False),
                            json.dumps(competitors, ensure_ascii=False), project_id))
        db().commit()
        return jsonify(project_row(project_id))

    @app.get("/api/projects/<int:project_id>/questions")
    def list_questions(project_id):
        project_row(project_id)
        return jsonify(questions_for(project_id))

    @app.post("/api/projects/<int:project_id>/questions")
    def add_question(project_id):
        require_real_project(project_id)
        payload = request.get_json(silent=True) or {}
        text = _clean_text(payload.get("text"), label="问题句", maximum=500, required=True)
        kind = str(payload.get("kind") or "keyword")
        if kind not in ("keyword", "brand"):
            raise ApiError("问题类型应为品类词或品牌词")
        try:
            cursor = db().execute("INSERT INTO questions (project_id,text,kind,created_at) VALUES (?,?,?,?)",
                                  (project_id, text, kind, _now()))
            db().commit()
        except sqlite3.IntegrityError:
            raise ApiError("该问题句已存在", 409) from None
        return jsonify(question_obj(db().execute("SELECT * FROM questions WHERE id=?", (cursor.lastrowid,)).fetchone())), 201

    @app.delete("/api/projects/<int:project_id>/questions/<int:question_id>")
    def remove_question(project_id, question_id):
        require_real_project(project_id)
        if db().execute("SELECT 1 FROM observations WHERE question_id=? LIMIT 1", (question_id,)).fetchone():
            raise ApiError("该问题已有监测记录，需先处理记录", 409)
        cursor = db().execute("DELETE FROM questions WHERE id=? AND project_id=?", (question_id, project_id))
        if not cursor.rowcount:
            raise ApiError("问题句不存在", 404)
        db().commit()
        return jsonify({"deleted": question_id})

    @app.get("/api/projects/<int:project_id>/observations")
    def list_observations(project_id):
        project_row(project_id)
        return jsonify(observations_for(project_id))

    @app.get("/api/evidence/<name>")
    def evidence_image(name):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.png", name):
            raise ApiError("截图文件名无效", 404)
        return send_from_directory(app.config["EVIDENCE_DIR"], name)

    @app.post("/api/projects/<int:project_id>/observations")
    def add_observation(project_id):
        require_real_project(project_id)
        data = normalize_observation(request.get_json(silent=True) or {}, project_id)
        observation_id = insert_observation(db(), project_id, data)
        db().commit()
        return jsonify(observation_obj(db().execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone())), 201

    @app.put("/api/projects/<int:project_id>/observations/<int:observation_id>")
    def update_observation(project_id, observation_id):
        require_real_project(project_id)
        old = db().execute("SELECT * FROM observations WHERE id=? AND project_id=?",
                           (observation_id, project_id)).fetchone()
        if not old:
            raise ApiError("监测记录不存在", 404)
        payload = {**observation_obj(old), **(request.get_json(silent=True) or {})}
        data = normalize_observation(payload, project_id)
        db().execute("""UPDATE observations SET question_id=?, platform=?, observed_at=?, status=?, answer=?,
            brand_mentioned=?, brand_rank=?, sentiment=?, competitor_ranks_json=?, citations_json=?,
            positive_terms_json=?, negative_terms_json=?, evidence_url=?, asked_at=?,
            page_url=?, evidence_image=?, error_message=?, rank_reviewed=? WHERE id=? AND project_id=?""",
            (data["question_id"], data["platform"], data["observed_at"], data["status"], data["answer"],
             int(data["brand_mentioned"]), data["brand_rank"], data["sentiment"],
             json.dumps(data["competitor_ranks"], ensure_ascii=False),
             json.dumps(data["citations"], ensure_ascii=False),
             json.dumps(data["positive_terms"], ensure_ascii=False),
             json.dumps(data["negative_terms"], ensure_ascii=False),
             data["evidence_url"], data["asked_at"], data["page_url"],
             data["evidence_image"], data["error_message"], int(data["rank_reviewed"]), observation_id, project_id))
        db().commit()
        return jsonify(observation_obj(db().execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()))

    @app.delete("/api/projects/<int:project_id>/observations/<int:observation_id>")
    def remove_observation(project_id, observation_id):
        require_real_project(project_id)
        cursor = db().execute("DELETE FROM observations WHERE id=? AND project_id=?",
                              (observation_id, project_id))
        if not cursor.rowcount:
            raise ApiError("监测记录不存在", 404)
        db().commit()
        return jsonify({"deleted": observation_id})

    @app.get("/api/projects/<int:project_id>/analytics")
    def analytics(project_id):
        project = project_row(project_id)
        start = _date(request.args["start"]) if request.args.get("start") else None
        end = _date(request.args["end"]) if request.args.get("end") else None
        if start and end and start > end:
            raise ApiError("开始日期不能晚于结束日期")
        result = build_analytics(project, questions_for(project_id), observations_for(project_id),
                                 start=start, end=end, platform=request.args.get("platform"),
                                 question_id=request.args.get("question_id"))
        return jsonify(result)

    @app.post("/api/projects/<int:project_id>/import")
    def import_csv(project_id):
        require_real_project(project_id)
        uploaded = request.files.get("file")
        if uploaded is None:
            raise ApiError("请选择 CSV 文件")
        try:
            content = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ApiError("CSV 需使用 UTF-8 编码") from None
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames or not {"question", "platform", "observed_at"}.issubset(reader.fieldnames):
            raise ApiError("CSV 至少需要 question、platform、observed_at 三列")
        rows = list(reader)
        if len(rows) > 2000:
            raise ApiError("单次最多导入 2000 行")
        conn = db()
        count = 0
        try:
            for index, row in enumerate(rows, 2):
                text = _clean_text(row.get("question"), label=f"第 {index} 行问题句", maximum=500, required=True)
                question = conn.execute("SELECT id FROM questions WHERE project_id=? AND text=?",
                                        (project_id, text)).fetchone()
                if question is None:
                    kind = row.get("question_type") or "keyword"
                    if kind not in ("keyword", "brand"):
                        raise ApiError(f"第 {index} 行问题类型无效")
                    qid = conn.execute("INSERT INTO questions (project_id,text,kind,created_at) VALUES (?,?,?,?)",
                                       (project_id, text, kind, _now())).lastrowid
                else:
                    qid = question["id"]
                payload = {"question_id": qid, "platform": row.get("platform"),
                           "observed_at": row.get("observed_at"), "status": row.get("status") or "complete",
                           "asked_at": row.get("asked_at"), "answer": row.get("answer"),
                           "evidence_url": row.get("evidence_url"), "page_url": row.get("page_url"),
                           "error_message": row.get("error_message"),
                           "brand_rank": row.get("brand_rank"), "rank_reviewed": row.get("rank_reviewed", ""),
                           "sentiment": row.get("sentiment") or "unknown",
                           "competitor_ranks": row.get("competitors") or "{}",
                           "citations": row.get("citations") or "[]",
                           "positive_terms": row.get("positive_terms") or "[]",
                           "negative_terms": row.get("negative_terms") or "[]"}
                if row.get("brand_mentioned", "").strip():
                    payload["brand_mentioned"] = row["brand_mentioned"]
                try:
                    data = normalize_observation(payload, project_id)
                except ApiError as exc:
                    raise ApiError(f"第 {index} 行：{exc}") from exc
                insert_observation(conn, project_id, data, "csv")
                count += 1
            conn.commit()
        except (ApiError, sqlite3.Error):
            conn.rollback()
            raise
        return jsonify({"imported": count})

    @app.get("/api/projects/<int:project_id>/export.csv")
    def export_csv(project_id):
        project_row(project_id)
        qmap = {q["id"]: q for q in questions_for(project_id)}
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        def cell(value):
            value = str(value)
            return "'" + value if value[:1] in ("=", "+", "-", "@") else value
        for row in observations_for(project_id):
            question = qmap.get(row["question_id"], {})
            writer.writerow({"question": cell(question.get("text", "")), "question_type": question.get("kind", "keyword"),
                             "platform": cell(row["platform"]), "observed_at": row["observed_at"],
                             "asked_at": row["asked_at"],
                             "status": row["status"], "answer": cell(row["answer"]),
                             "evidence_url": cell(row["evidence_url"]), "page_url": cell(row["page_url"]),
                             "evidence_image": cell(row["evidence_image"]),
                             "error_message": cell(row["error_message"]),
                             "brand_mentioned": int(row["brand_mentioned"]), "brand_rank": row["brand_rank"] or "",
                             "sentiment": row["sentiment"], "competitors": json.dumps(row["competitor_ranks"], ensure_ascii=False),
                             "citations": json.dumps(row["citations"], ensure_ascii=False),
                             "positive_terms": json.dumps(row["positive_terms"], ensure_ascii=False),
                             "negative_terms": json.dumps(row["negative_terms"], ensure_ascii=False),
                             "rank_reviewed": int(row["rank_reviewed"])})
        return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="geo-project-{project_id}.csv"'})

    @app.get("/api/projects/<int:project_id>/export.zip")
    def export_bundle(project_id):
        project = project_row(project_id)
        rows = observations_for(project_id)
        evidence_root = Path(app.config["EVIDENCE_DIR"]).resolve()
        files = {}
        missing = []
        for row in rows:
            name = row["evidence_image"]
            if not name:
                continue
            path = (evidence_root / name).resolve()
            if path.parent != evidence_root or not path.is_file():
                missing.append(name)
                continue
            files[name] = path
        if sum(p.stat().st_size for p in files.values()) > 100 * 1024 * 1024:
            raise ApiError("证据文件超过 100 MB，请先按项目整理后再导出")
        manifest = {"format": "geo-evidence-1", "project": project,
                    "questions": questions_for(project_id), "observations": rows,
                    "missing_evidence": missing}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("project.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("observations.csv", export_csv(project_id).get_data())
            archive.writestr("README.txt", "GEO 本轮样本与证据。原回答见 project.json 和 observations.csv；截图见 evidence。\n"
                              "本包不含浏览器登录资料。排名、评价和引用需复核；PDF 请从报告页打印保存。\n")
            for name, path in files.items():
                archive.write(path, "evidence/" + name)
        return Response(output.getvalue(), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="geo-project-{project_id}-evidence.zip"'})

    @app.get("/api/template.csv")
    def csv_template():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(CSV_COLUMNS)
        writer.writerow(["某品类有哪些值得推荐的品牌？", "keyword", "豆包", "2026-10-21", "",
                         "complete", "【示例】某品牌被提及。", "", "", "", "", "1", "",
                         "unknown", "{}", "[]", "[]", "[]"])
        return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="geo-import-template.csv"'})

    def providers():
        file_path = os.getenv("GEO_PROVIDER_CONFIG_FILE", "")
        if not file_path:
            return {}
        try:
            raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @app.get("/api/providers")
    def list_providers():
        result = []
        for name, cfg in providers().items():
            if not isinstance(cfg, dict):
                continue
            result.append({"name": name, "model": cfg.get("model", ""),
                           "ready": bool(cfg.get("key_env") and os.getenv(cfg["key_env"]))})
        return jsonify(result)

    @app.get("/api/browser-platforms")
    def list_browser_platforms():
        local = is_local()
        return jsonify({"ready": browser_ready() if local else guest_remote_ready(),
                        "runtime": "local" if local else "remote", "platforms": BROWSER_PLATFORMS})

    @app.get("/api/browser-accounts")
    def list_browser_accounts():
        if is_local():
            return jsonify(ready=browser_ready(), runtime="local", accounts=[])
        try:
            accounts = list_guest_accounts()
        except BrowserCollectionError as exc:
            return jsonify(ready=False, accounts=[], error=str(exc)), 503
        return jsonify(ready=True, accounts=accounts)

    @app.post("/api/projects/<int:project_id>/run-browser")
    def run_browser_probe(project_id):
        require_real_project(project_id)
        local = is_local()
        if not (browser_ready() if local else guest_remote_ready()):
            raise ApiError("本机浏览器依赖尚未安装" if local else "浏览器采集机尚未配置")
        payload = request.get_json(silent=True) or {}
        platform = payload.get("platform")
        if platform not in BROWSER_PLATFORMS:
            raise ApiError("尚未支持该网页平台")
        participant_id = None
        if not local:
            participant_id = _clean_text(payload.get("participant_id"), label="登录账号", maximum=32, required=True)
            if not re.fullmatch(r"[a-f0-9]{32}", participant_id):
                raise ApiError("登录账号编号无效")
        try:
            ids = [int(qid) for qid in payload.get("question_ids", [])]
        except (TypeError, ValueError):
            raise ApiError("问题句编号无效") from None
        if not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids):
            raise ApiError("每次请选择 1 到 3 条不同的问题句")
        qmap = {q["id"]: q for q in questions_for(project_id)}
        if any(qid not in qmap for qid in ids):
            raise ApiError("包含不属于当前项目的问题句")
        try:
            remote_rows = collect_remote(
                platform, [{"id": qid, "text": qmap[qid]["text"]} for qid in ids], participant_id
            )
        except BrowserCollectionError as exc:
            remote_rows = [{"question_id": qid, "status": "error",
                            "error_message": str(exc)} for qid in ids]
        remote_map = {row.get("question_id"): row for row in remote_rows if isinstance(row, dict)}
        results = []
        for qid in ids:
            row = remote_map.get(qid, {})
            raw_image = row.get("screenshot_base64", "")
            screenshot = b""
            if isinstance(raw_image, str) and raw_image:
                try:
                    screenshot = base64.b64decode(raw_image, validate=True)
                except (ValueError, binascii.Error):
                    screenshot = b""
            if not screenshot.startswith(b"\x89PNG\r\n\x1a\n") or len(screenshot) > 5 * 1024 * 1024:
                screenshot = b""
            answer = row.get("answer") if isinstance(row.get("answer"), str) else ""
            answer = answer[:30000]
            asked_at = str(row.get("asked_at") or "")
            complete = row.get("status") == "complete" and bool(answer.strip() and asked_at and screenshot)
            message = "" if complete else str(row.get("error_message") or "未取得完整回答与截图")[:500]
            image_name = f"p{project_id}-q{qid}-{secrets.token_hex(8)}.png" if screenshot else ""
            if screenshot:
                evidence_dir = Path(app.config["EVIDENCE_DIR"])
                evidence_dir.mkdir(parents=True, exist_ok=True)
                (evidence_dir / image_name).write_bytes(screenshot)
            try:
                timestamp = _timestamp(asked_at) if asked_at else ""
            except ApiError:
                timestamp = ""
                complete = False
                message = "采集机返回的提问时间无效"
            observed_at = (datetime.fromisoformat(timestamp).astimezone(
                ZoneInfo("Asia/Shanghai")).date().isoformat() if timestamp else date.today().isoformat())
            page_url = str(row.get("page_url") or "")[:2000]
            if urlparse(page_url).scheme not in ("http", "https"):
                page_url = ""
            evidence_url = str(row.get("evidence_url") or "")[:2000]
            if urlparse(evidence_url).scheme not in ("http", "https"):
                evidence_url = ""
            normalized = normalize_observation({
                "question_id": qid, "platform": platform, "observed_at": observed_at,
                "asked_at": timestamp, "status": "complete" if complete else "error",
                "answer": answer, "page_url": page_url,
                "evidence_url": evidence_url,
                "evidence_image": image_name, "error_message": message,
            }, project_id)
            observation_id = insert_observation(db(), project_id, normalized, "browser")
            results.append({"question_id": qid, "status": normalized["status"],
                            "observation_id": observation_id, "message": message})
        db().commit()
        return jsonify({"results": results})

    @app.post("/api/projects/<int:project_id>/run")
    def run_probe(project_id):
        require_real_project(project_id)
        payload = request.get_json(silent=True) or {}
        name = payload.get("provider")
        cfg = providers().get(name)
        if not isinstance(cfg, dict):
            raise ApiError("该模型接口尚未配置")
        url = cfg.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
            raise ApiError("模型接口须使用 HTTPS，或指向本机")
        token = os.getenv(cfg["key_env"], "") if cfg.get("key_env") else ""
        if not token:
            raise ApiError("模型接口密钥尚未通过环境变量配置")
        try:
            ids = [int(qid) for qid in payload.get("question_ids", [])]
        except (TypeError, ValueError):
            raise ApiError("问题句编号无效") from None
        if not 1 <= len(ids) <= 5:
            raise ApiError("每次请选择 1 到 5 条问题句")
        qmap = {q["id"]: q for q in questions_for(project_id)}
        if any(qid not in qmap for qid in ids):
            raise ApiError("包含不属于当前项目的问题句")
        results = []
        for qid in ids:
            body = json.dumps({"model": cfg.get("model"), "messages": [
                {"role": "system", "content": "请像通常回答用户问题一样，给出有依据的中文回答；不因本次监测偏向任何品牌。"},
                {"role": "user", "content": qmap[qid]["text"]}], "temperature": 0.2}, ensure_ascii=False).encode("utf-8")
            req = urlrequest.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            try:
                with urlrequest.urlopen(req, timeout=30) as response:
                    result = json.load(response)
                answer = result["choices"][0]["message"]["content"]
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("模型没有返回文本")
                normalized = normalize_observation({"question_id": qid, "platform": name,
                    "observed_at": date.today().isoformat(), "answer": answer, "status": "complete"}, project_id)
                oid = insert_observation(db(), project_id, normalized, "provider")
                results.append({"question_id": qid, "status": "complete", "observation_id": oid})
            except (urlerror.URLError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
                results.append({"question_id": qid, "status": "error", "message": str(exc)[:180]})
        db().commit()
        return jsonify({"results": results})

    initialize()
    return app


def seed_demo(conn):
    """A plainly marked fictional dataset for inspecting the interface."""
    created = _now()
    brand = "星河工程咨询（演示）"
    competitors = ["远山顾问（演示）", "青岳咨询（演示）", "恒桥工程（演示）", "海岚顾问（演示）"]
    project_id = conn.execute("""INSERT INTO projects
        (brand,category,region,aliases_json,competitors_json,is_demo,created_at)
        VALUES (?,?,?,?,?,1,?)""",
        (brand, "工程咨询", "北京", "[]", json.dumps(competitors, ensure_ascii=False), created)).lastrowid
    qids = []
    for kind, text in default_questions(brand, "工程咨询", "北京"):
        qids.append(conn.execute("INSERT INTO questions (project_id,text,kind,created_at) VALUES (?,?,?,?)",
                                 (project_id, text, kind, created)).lastrowid)
    platforms = ["DP", "豆包", "Kimi", "千问", "文心一言", "元宝"]
    for index in range(24):
        rank = [None, 1, 2, None, 4, None, 3, None][index % 8]
        mentioned = rank is not None or index % 5 == 0
        status = "offline" if index in (7, 19) else "complete"
        if status == "offline":
            rank, mentioned = None, False
        answer = (f"【演示回答】关于该问题，{brand}在模拟样本中被提及；"
                  "真实诊断应保存模型原文和采集时间。") if mentioned else (
                  "【演示回答】此条模拟回答未提及目标品牌，仅用于展示统计和记录流程。")
        if status == "offline":
            answer = ""
        sentiment = ["positive", "neutral", "negative", "unknown"][index % 4] if mentioned else "unknown"
        rival = competitors[index % len(competitors)]
        rivals = {rival: [1, 2, 4, None][index % 4]}
        citations = [] if index % 3 == 0 else [{"title": "演示资料：工程咨询选择指南",
            "source": "演示来源 A" if index % 2 else "演示来源 B", "url": ""}]
        conn.execute("""INSERT INTO observations
            (project_id,question_id,platform,observed_at,status,answer,brand_mentioned,brand_rank,
             sentiment,competitor_ranks_json,citations_json,positive_terms_json,negative_terms_json,
             source_kind,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, qids[index % len(qids)], platforms[index % len(platforms)],
             f"2026-10-{15 + (index % 7):02d}", status, answer, int(mentioned), rank,
             sentiment, json.dumps(rivals, ensure_ascii=False), json.dumps(citations, ensure_ascii=False),
             json.dumps(["专业响应", "服务清晰"] if sentiment == "positive" else [], ensure_ascii=False),
             json.dumps(["信息不足"] if sentiment == "negative" else [], ensure_ascii=False),
             "demo", created))


if __name__ == "__main__":
    application = create_app()
    application.run(host="127.0.0.1", port=int(os.getenv("GEO_PORT", "8765")), debug=False)
