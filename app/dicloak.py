import json
import os
import threading
import time
import httpx
from datetime import datetime, timezone
import secrets
from . import config

_lock = threading.Lock()
_cache = {"data": None, "mtime": None}

DICLOAK_API_URL = "https://app.dicloak.com/gin/v1/api/member/open?token=I-KvzBRCFDY-JBsp&id=2098479368974073858"

def _now() -> int:
    return int(time.time())

def _path() -> str:
    return os.path.join(config.DATA_DIR, "dicloak_users.json")

def _load_raw() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("users", {})
            return data
    except (FileNotFoundError, ValueError, OSError):
        return {"users": {}}

def _save(data: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

def create_user(name: str, days: int):
    password = secrets.token_urlsafe(12)
    
    # Payload for dicloak API
    payload = {
        "name": name,
        "passwd": password,
        "authority": "MEMBER",
        "type": "EXTERNAL",
        "status": "ENABLE"
    }
    
    try:
        resp = httpx.post(DICLOAK_API_URL, json=payload, timeout=10.0)
        resp_data = resp.json()
    except Exception as e:
        return False, str(e), None

    with _lock:
        data = _load_raw()
        user_id = secrets.token_hex(8)
        data["users"][user_id] = {
            "name": name,
            "password": password,
            "created_at": _now(),
            "expires_at": _now() + days * 86400,
            "api_response": resp_data,
            "status": "active"
        }
        _save(data)
        
    return True, "Success", {"name": name, "password": password, "days": days}

def list_users() -> list:
    data = _load_raw()
    return list(data.get("users", {}).items())

def search_users(username: str) -> list:
    username = username.lower()
    data = _load_raw()
    res = []
    for uid, u in data.get("users", {}).items():
        if username in u.get("name", "").lower():
            res.append((uid, u))
    return res

def extend_user(uid: str, days: int) -> bool:
    with _lock:
        data = _load_raw()
        if uid in data.get("users", {}):
            u = data["users"][uid]
            # if already expired, start from now
            current_exp = u.get("expires_at", _now())
            if current_exp < _now():
                current_exp = _now()
            u["expires_at"] = current_exp + (days * 86400)
            _save(data)
            return True
        return False
