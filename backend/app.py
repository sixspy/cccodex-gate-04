import os

from flask import Flask, jsonify, request

from auth import authenticate, issue_token, read_user
from db import GateError, connect, dict_overview, init_db, rebuild_day, save_dict_settings

app = Flask(__name__)
app.json.ensure_ascii = False


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _user():
    return read_user(request.headers.get("Authorization"))


def _reader():
    return _user() is not None


def _writer():
    user = _user()
    if not user:
        return None
    if user["role"] != "writer":
        return None
    return user


@app.errorhandler(GateError)
def _gate_error(error):
    body = {"detail": error.detail}
    body.update(error.data or {})
    return jsonify(body), error.status


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _reader():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.biz_date,
                   (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
                   (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
                   (SELECT COUNT(*) FROM dict_unknown u WHERE u.biz_date = d.biz_date) AS dict_count,
                   EXISTS(SELECT 1 FROM build_runs r WHERE r.biz_date = d.biz_date) AS built
            FROM (SELECT DISTINCT biz_date FROM ods_scan) d
            ORDER BY d.biz_date
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
    return jsonify(rows)


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _reader():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        ods = conn.execute(
            """
            SELECT id, terminal_code, raw_payload
            FROM ods_scan WHERE biz_date = %s ORDER BY id
            """,
            (biz_date,),
        ).fetchall()
        if not ods:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        moves = conn.execute(
            """
            SELECT source_id, terminal_code, container_no, event_kind, event_at, raw_event_code
            FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id
            """,
            (biz_date,),
        ).fetchall()
        run = conn.execute(
            """
            SELECT id, accepted_count, rejected_count, built_by, built_at
            FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
            """,
            (biz_date,),
        ).fetchone()
        rejects = []
        if run:
            rejects = conn.execute(
                """
                SELECT source_id, reason, reason_code, event_code FROM build_rejects
                WHERE run_id = %s ORDER BY source_id
                """,
                (run["id"],),
            ).fetchall()
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
    if run:
        run["built_at"] = _stamp(run["built_at"])
    return jsonify(
        {
            "biz_date": biz_date,
            "ods": ods,
            "moves": moves,
            "run": run,
            "rejects": rejects,
        }
    )


@app.get("/api/dates/<biz_date>/dict")
def read_dict(biz_date):
    if not _reader():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        return jsonify(dict_overview(conn, biz_date))


@app.post("/api/dates/<biz_date>/dict")
def write_dict(biz_date):
    user = _writer()
    if not user:
        return jsonify({"detail": "未登录或没有权限"}), 401 if not _user() else 403
    body = request.get_json(silent=True) or {}
    items = body.get("items")
    if items is None and "code" in body:
        items = [body]
    if not isinstance(items, list) or not items:
        return jsonify({"detail": "要给出事件码和清洗办法"}), 400
    with connect() as conn:
        result, status = save_dict_settings(conn, biz_date, items, user["username"])
        conn.commit()
        result["overview"] = dict_overview(conn, biz_date)
    if status == 409:
        result["detail"] = "、".join(
            f"{item['code']}：{item['reason']}" for item in result["conflicts"]
        )
    return jsonify(result), status


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user = _writer()
    if not user:
        return jsonify({"detail": "未登录或没有权限"}), 401 if not _user() else 403
    body = request.get_json(silent=True) or {}
    methods = body.get("clean_methods")
    if methods is not None and not isinstance(methods, dict):
        return jsonify({"detail": "清洗办法要按事件码给出"}), 400
    with connect() as conn:
        rebuild_day(conn, biz_date, user["username"], methods)
        conn.commit()
    return date_detail(biz_date)


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
