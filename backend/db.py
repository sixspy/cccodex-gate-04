import json
import os
import time
import zlib

import psycopg
from psycopg.rows import dict_row

from build import (
    KIND,
    METHOD_NAMES,
    METHOD_REJECT,
    UNKNOWN_KIND,
    clean_scan,
    method_key,
    scan_code,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS build_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    built_by TEXT NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES build_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);

-- 未知事件码的清洗办法。一天一码只定一次，定下后本日禁止改口。
CREATE TABLE IF NOT EXISTS dict_unknown (
    biz_date DATE NOT NULL,
    code TEXT NOT NULL,
    method TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (biz_date, code)
);

-- 清洗办法的落定过程：采纳、争用、被拒的改口都留一笔。
CREATE TABLE IF NOT EXISTS dict_attempts (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    code TEXT NOT NULL,
    method TEXT,
    actor TEXT NOT NULL,
    outcome TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE dwd_move ADD COLUMN IF NOT EXISTS raw_event_code TEXT NOT NULL DEFAULT '';
ALTER TABLE build_rejects ADD COLUMN IF NOT EXISTS reason_code TEXT NOT NULL DEFAULT '';
ALTER TABLE build_rejects ADD COLUMN IF NOT EXISTS event_code TEXT NOT NULL DEFAULT '';
"""

REASON_EMPTY = "事件码为空，不进未知队列也不收成"
REASON_DICT_REJECT = "字典定为整条剔除，不进明细"
REASON_MISMATCH = "字典清洗不符"
REASON_BOX = "箱号不是四位字母加七位数字"

OUTCOME_LABEL = {
    "applied": "已写进字典设置",
    "unchanged": "与字典设置一致",
    "contention": "字典争用",
    "mismatch": "字典清洗不符",
    "change_refused": "本日禁止改口",
}

# 咨询锁命名空间：1 = 某天某码的字典设置；2 = 某一天的收成。
LOCK_SETTINGS = 1
LOCK_BUILD = 2


class GateError(Exception):
    """带 HTTP 状态码的业务回绝，由接口层转成 detail。"""

    def __init__(self, detail, status=409, data=None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.data = data or {}


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _land(conn, biz_date, terminal, payload):
    conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, raw_payload)
        VALUES (%s, %s, %s::jsonb)
        """,
        (biz_date, terminal, json.dumps(payload, ensure_ascii=False)),
    )


def _seed(conn):
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1"},
    ]
    waiting = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2"},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload)
    for payload in waiting:
        _land(conn, "2026-11-12", payload["gate"], payload)
    rebuild_day(conn, "2026-11-11", "admin")


# ---------------------------------------------------------------- 咨询锁


def _key(tag: int, text: str) -> tuple[int, int]:
    return tag, zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


def _try_lock(conn, tag: int, text: str) -> bool:
    return conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s) AS ok",
        _key(tag, text),
    ).fetchone()["ok"]


def _wait_lock(conn, tag: int, text: str):
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", _key(tag, text))


def _settings_lock(conn, biz_date: str, code: str) -> bool:
    return _try_lock(conn, LOCK_SETTINGS, f"{biz_date}:{code}")


# ---------------------------------------------------------------- 读取辅助


def _payload(row) -> dict:
    payload = row["raw_payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _rows_of_day(conn, biz_date):
    return conn.execute(
        "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()


def _unknown_codes(rows):
    """字典认不出的、且非空的事件码。空事件码不进未知队列。"""
    return sorted(
        {
            code
            for row in rows
            for code in [scan_code(_payload(row))]
            if code and code not in KIND
        }
    )


def _known_settings(conn, biz_date, codes):
    if not codes:
        return {}
    rows = conn.execute(
        """
        SELECT code, method, set_by, set_at FROM dict_unknown
        WHERE biz_date = %s AND code = ANY(%s)
        """,
        (biz_date, sorted(codes)),
    ).fetchall()
    return {row["code"]: row for row in rows}


def _log_attempt(conn, biz_date, code, method, actor, outcome, note=""):
    conn.execute(
        """
        INSERT INTO dict_attempts (biz_date, code, method, actor, outcome, note)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (biz_date, code, method, actor, outcome, note),
    )


def _latest_run(conn, biz_date):
    return conn.execute(
        """
        SELECT id, accepted_count, rejected_count, built_by, built_at
        FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
        """,
        (biz_date,),
    ).fetchone()


# ---------------------------------------------------------------- 字典设置


def save_dict_settings(conn, biz_date, items, actor):
    """落定未知码清洗办法。一天一码只采纳一笔，与已定办法一致才算一致。

    返回 (结果, HTTP 状态)。落定过程和争用记录都写进清洗详情；
    全部争用/禁止改口时给 409，一笔都没进字典设置。
    """
    rows = _rows_of_day(conn, biz_date)
    if not rows:
        raise GateError("这一天没有贴源扫描", 404)
    codes_today = {
        code for row in rows for code in [scan_code(_payload(row))] if code
    }

    applied, conflicts = [], []
    # 先全部校验一遍，再逐个加锁落定；校验不过就整笔回绝，不留半截设置。
    prepared = []
    for item in items:
        item = item if isinstance(item, dict) else {}
        code = scan_code({"scan": item.get("code")})
        method = method_key(item.get("method"))
        if not code:
            raise GateError("事件码不能为空", 400)
        if code in KIND:
            raise GateError(f"{code} 是已登记事件，不用定清洗办法", 400)
        if code not in codes_today:
            raise GateError(f"这一天的贴源扫描里没有事件码 {code}", 400)
        if not method:
            raise GateError(
                f"事件码 {code} 的清洗办法只能选整条剔除或改写为未分类事件", 400
            )
        prepared.append((code, method))
    # 按事件码排序后再逐个加锁，多个码一起提交时锁序一致，不会互相等死。
    prepared.sort(key=lambda pair: pair[0])

    for code, method in prepared:
        # 加锁前后各看一眼字典设置。锁前还没有、锁后有了，说明另一笔
        # 同时到达的清洗先落定了，这一笔记字典争用或与设置一致；
        # 锁前就已经有设置，说明是落定之后的改口，记本日禁止改口。
        before = conn.execute(
            "SELECT method FROM dict_unknown WHERE biz_date = %s AND code = %s",
            (biz_date, code),
        ).fetchone()
        waited = False
        if not _settings_lock(conn, biz_date, code):
            _wait_lock(conn, LOCK_SETTINGS, f"{biz_date}:{code}")
            waited = True
        existing = conn.execute(
            "SELECT method FROM dict_unknown WHERE biz_date = %s AND code = %s",
            (biz_date, code),
        ).fetchone()
        raced = waited and before is None
        if existing and existing["method"] == method:
            note = (
                "同时到达的另一笔先落定，与字典设置一致"
                if raced
                else "与字典设置一致，本日不改口"
            )
            _log_attempt(conn, biz_date, code, method, actor, "unchanged", note)
            applied.append(
                {
                    "code": code,
                    "method": method,
                    "method_name": METHOD_NAMES[method],
                    "changed": False,
                }
            )
            continue
        if existing:
            if raced:
                outcome = "contention"
                reason = f"同时到达的另一笔已落定{METHOD_NAMES[existing['method']]}"
            else:
                outcome = "change_refused"
                reason = f"本日已定{METHOD_NAMES[existing['method']]}，禁止改口"
            _log_attempt(conn, biz_date, code, method, actor, outcome, reason)
            conflicts.append({"code": code, "method": method, "reason": reason})
            continue
        conn.execute(
            """
            INSERT INTO dict_unknown (biz_date, code, method, set_by)
            VALUES (%s, %s, %s, %s)
            """,
            (biz_date, code, method, actor),
        )
        _log_attempt(conn, biz_date, code, method, actor, "applied", "本日清洗办法落定")
        applied.append(
            {
                "code": code,
                "method": method,
                "method_name": METHOD_NAMES[method],
                "changed": True,
            }
        )

    result = {"biz_date": biz_date, "applied": applied, "conflicts": conflicts}
    if conflicts and not applied:
        return result, 409
    return result, 200


def dict_overview(conn, biz_date):
    rows = _rows_of_day(conn, biz_date)
    if not rows:
        raise GateError("这一天没有贴源扫描", 404)
    unknown_codes = _unknown_codes(rows)
    counts = {code: 0 for code in unknown_codes}
    for row in rows:
        code = scan_code(_payload(row))
        if code in counts:
            counts[code] += 1

    run = _latest_run(conn, biz_date)
    rejected, rewritten = {}, {}
    if run:
        # 剔除条数按码统计本笔收成里没进明细的扫描；改写条数只算改写成
        # 未分类事件后进了明细的扫描。
        for row in conn.execute(
            """
            SELECT event_code, COUNT(*) AS n FROM build_rejects
            WHERE run_id = %s AND event_code <> ''
            GROUP BY event_code
            """,
            (run["id"],),
        ).fetchall():
            rejected[row["event_code"]] = row["n"]
        for row in conn.execute(
            """
            SELECT raw_event_code, COUNT(*) AS n FROM dwd_move
            WHERE biz_date = %s AND event_kind = %s AND raw_event_code <> ''
            GROUP BY raw_event_code
            """,
            (biz_date, UNKNOWN_KIND),
        ).fetchall():
            rewritten[row["raw_event_code"]] = row["n"]

    settings = _known_settings(conn, biz_date, unknown_codes)
    unknown = []
    for code in unknown_codes:
        setting = settings.get(code)
        method = setting["method"] if setting else None
        unknown.append(
            {
                "code": code,
                "method": method,
                "method_name": METHOD_NAMES.get(method) if method else None,
                "scan_count": counts.get(code, 0),
                "rejected_count": rejected.get(code, 0),
                "rewritten_count": rewritten.get(code, 0),
                "locked": setting is not None,
            }
        )

    attempts = conn.execute(
        """
        SELECT code, method, actor, outcome, note, at FROM dict_attempts
        WHERE biz_date = %s ORDER BY id
        """,
        (biz_date,),
    ).fetchall()
    for row in attempts:
        row["method_name"] = METHOD_NAMES.get(row["method"]) if row["method"] else None
        row["outcome_label"] = OUTCOME_LABEL.get(row["outcome"], row["outcome"])
        row["at"] = _stamp(row["at"])

    return {
        "biz_date": biz_date,
        "registered": [
            {"code": code, "name": name, "group": "registered"}
            for code, name in KIND.items()
        ],
        "unknown": unknown,
        "attempts": attempts,
        "built": run is not None,
    }


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


# ---------------------------------------------------------------- 收成


def _reject(conn, run_id, source_id, reason, reason_code, event_code):
    conn.execute(
        """
        INSERT INTO build_rejects (run_id, source_id, reason, reason_code, event_code)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (run_id, source_id, reason, reason_code, event_code),
    )


def _insert_move(conn, biz_date, source_id, terminal, cleaned, raw_code):
    conn.execute(
        """
        INSERT INTO dwd_move
            (biz_date, source_id, terminal_code, container_no, event_kind, event_at, raw_event_code)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            biz_date,
            source_id,
            terminal,
            cleaned["container_no"],
            cleaned["event_kind"],
            cleaned["event_at"],
            raw_code,
        ),
    )


def rebuild_day(conn, biz_date: str, username: str, clean_methods=None):
    """按字典把一天的扫描收成明细层。

    未知事件码必须先有字典设置；收成请求带来的清洗办法只用来核对，
    与设置偏离就记字典清洗不符，该码不落库。同一天的收成串行执行，
    另一笔正在收成时后到的一笔等它落定再按字典设置收。
    """
    requested = {}
    for raw_code, raw_method in (clean_methods or {}).items():
        code = scan_code({"scan": raw_code})
        if not code or code in KIND:
            continue
        method = method_key(raw_method)
        if not method:
            raise GateError(
                f"事件码 {code} 的清洗办法认不出，只能选整条剔除或改写为未分类事件",
                400,
            )
        requested[code] = method

    _wait_lock(conn, LOCK_BUILD, biz_date)

    rows = _rows_of_day(conn, biz_date)
    if not rows:
        raise GateError("这一天没有贴源扫描", 404)
    parsed = [(row, _payload(row), scan_code(_payload(row))) for row in rows]
    unknown = _unknown_codes(rows)

    for code in unknown:
        if not _settings_lock(conn, biz_date, code):
            # 字典还在保存，这一码暂不准收成；整笔不落库。
            raise GateError(f"字典还在保存，事件码 {code} 暂不能收成", 409)

    settings = _known_settings(conn, biz_date, unknown)
    missing = [code for code in unknown if code not in settings]
    if missing:
        raise GateError(
            "收成明细前要定清洗办法，这些未知事件码还没写进字典设置："
            + "、".join(missing),
            409,
        )

    # 每个未知码先定下本笔收成的去留，避免逐条重复判断和记录。
    plan = {}
    for code in unknown:
        method = settings[code]["method"]
        wanted = requested.get(code)
        if wanted and wanted != method:
            _log_attempt(
                conn, biz_date, code, wanted, username, "mismatch",
                f"字典设置为{METHOD_NAMES.get(method, method)}",
            )
            plan[code] = ("reject", REASON_MISMATCH, "dict_mismatch")
        elif method == METHOD_REJECT:
            plan[code] = ("reject", REASON_DICT_REJECT, "dict_reject")
        else:
            plan[code] = ("rewrite", None, None)

    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    run = conn.execute(
        """
        INSERT INTO build_runs (biz_date, accepted_count, rejected_count, built_by)
        VALUES (%s, 0, 0, %s)
        RETURNING id
        """,
        (biz_date, username),
    ).fetchone()

    accepted = 0
    rejected = 0
    for row, payload, code in parsed:
        source_id = row["id"]
        if not code:
            _reject(conn, run["id"], source_id, REASON_EMPTY, "empty_code", "")
            rejected += 1
            continue

        if code in KIND:
            cleaned, reason = clean_scan(payload, KIND[code])
            if reason:
                reason_code = "bad_box" if reason == REASON_BOX else "bad_time"
                _reject(conn, run["id"], source_id, reason, reason_code, code)
                rejected += 1
                continue
            _insert_move(conn, biz_date, source_id, row["terminal_code"], cleaned, code)
            accepted += 1
            continue

        action, reason, reason_code = plan[code]
        if action == "reject":
            _reject(conn, run["id"], source_id, reason, reason_code, code)
            rejected += 1
            continue

        cleaned, reason = clean_scan(payload, UNKNOWN_KIND)
        if reason:
            reason_code = "bad_box" if reason == REASON_BOX else "bad_time"
            _reject(conn, run["id"], source_id, reason, reason_code, code)
            rejected += 1
            continue
        _insert_move(conn, biz_date, source_id, row["terminal_code"], cleaned, code)
        accepted += 1

    conn.execute(
        "UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s",
        (accepted, rejected, run["id"]),
    )
    return run["id"]
