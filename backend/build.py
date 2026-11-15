import re
from datetime import datetime

KIND = {"IN": "进闸", "OUT": "出闸", "RS": "倒箱"}
UNKNOWN_KIND = "未分类事件"

METHOD_REJECT = "reject"
METHOD_REWRITE = "rewrite"
METHOD_NAMES = {
    METHOD_REJECT: "整条剔除",
    METHOD_REWRITE: "改写为未分类事件",
}

# 接口既收英文办法码，也收页面上写的中文办法名。
METHOD_ALIASES = {
    "整条剔除": METHOD_REJECT,
    "剔除": METHOD_REJECT,
    "丢弃": METHOD_REJECT,
    "改写": METHOD_REWRITE,
    "未分类事件": METHOD_REWRITE,
    "改写为未分类事件": METHOD_REWRITE,
}

BOX = re.compile(r"^[A-Z]{4}\d{7}$")


def scan_code(payload: dict) -> str:
    """事件码统一成大写、去空白。空事件码返回空串。"""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("scan") or "").strip().upper()


def method_key(value) -> str | None:
    """把请求里的清洗办法归一成 reject / rewrite。认不出返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in (METHOD_REJECT, METHOD_REWRITE):
        return lowered
    return METHOD_ALIASES.get(text)


def clean_scan(payload: dict, event_kind: str):
    """校验箱号和时间，返回要落明细的一行。event_kind 由字典或清洗办法决定。"""
    if not isinstance(payload, dict):
        payload = {}
    box = str(payload.get("box") or "").strip().upper()
    stamp = str(payload.get("at") or "").strip()
    if not BOX.match(box):
        return None, "箱号不是四位字母加七位数字"
    try:
        event_at = datetime.fromisoformat(stamp)
    except ValueError:
        return None, "扫描时间无法读取"
    return {
        "container_no": box,
        "event_kind": event_kind,
        "event_at": event_at,
    }, None
