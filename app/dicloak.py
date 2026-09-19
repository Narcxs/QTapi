import json
import os
import threading
import time
import urllib.parse
import httpx
from datetime import datetime, timezone
import secrets
from . import config

_lock = threading.Lock()
_cache = {"data": None, "mtime": None}

PACK_URLS = {
    "ultra": "https://app.dicloak.com/gin/v1/api/member/open?token=I-KvzBRCFDY-JBsp&id=2098479368974073858",
    "basic": "https://app.dicloak.com/gin/v1/api/member/open?token=I-KvzBRCFDY-JBsp&id=2098487691383373826",
    "spotgamma": "https://app.dicloak.com/gin/v1/api/member/open?token=I-KvzBRCFDY-JBsp&id=2099397059477995522",
    "quantdata": "https://app.dicloak.com/gin/v1/api/member/open?token=I-KvzBRCFDY-JBsp&id=2101237914929123329"
}

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

def _generate_password(length=12):
    import string
    import random
    chars = string.ascii_letters + string.digits
    while True:
        pwd = ''.join(random.choice(chars) for _ in range(length))
        if (any(c.islower() for c in pwd) and 
            any(c.isupper() for c in pwd) and 
            any(c.isdigit() for c in pwd)):
            return pwd

def create_user(name: str, days: int, pack: str = "ultra"):
    password = _generate_password(12)
    # Dicloak account must probably be alphanumeric and at least 6 characters long
    account = "".join(c for c in name if c.isalnum()).lower()
    if len(account) < 6:
        account += secrets.token_hex(3)
        
    # URL for dicloak API (GET request)
    base_url = PACK_URLS.get(pack)
    if not base_url:
        return False, "Pack invalide", None
        
    q_name = urllib.parse.quote(name)
    q_acc = urllib.parse.quote(account)
    q_pwd = urllib.parse.quote(password)
    
    url = f"{base_url}&name={q_name}&account={q_acc}&password={q_pwd}&remark=telegram&days={days}"
    
    try:
        resp = httpx.get(url, timeout=10.0)
        # Check if it's JSON
        try:
            resp_data = resp.json()
        except Exception:
            resp_data = resp.text
            
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {str(resp_data)[:100]}", None
            
        if isinstance(resp_data, dict):
            code = resp_data.get('code')
            msg = resp_data.get('msg', '')
            # Si le code existe et n'est ni 0 ni 200, c'est une erreur d'API
            if code is not None and str(code) not in ("0", "200"):
                return False, f"Erreur API Dicloak ({code}): {msg}", None
            # S'il n'y a pas de code mais un message d'erreur
            if not code and "error" in msg.lower():
                return False, f"Erreur API Dicloak : {msg}", None
            
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
            "status": "active",
            "pack": pack,
            "account": account
        }
        _save(data)
        
    return True, "Success", {"id": user_id, "name": name, "account": account, "password": password, "days": days, "pack": pack}

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
    import urllib.parse
    import httpx
    from datetime import datetime, timezone
    
    with _lock:
        data = _load_raw()
        if uid in data.get("users", {}):
            u = data["users"][uid]
            # if already expired, start from now
            current_exp = u.get("expires_at", _now())
            if current_exp < _now():
                current_exp = _now()
            
            new_exp = current_exp + (days * 86400)
            u["expires_at"] = new_exp
            
            # Appeler l'API Dicloak pour prolonger
            member_id = u.get("api_response", {}).get("data", {}).get("member_id")
            if member_id:
                try:
                    disuse_time = datetime.fromtimestamp(new_exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    token = "I-KvzBRCFDY-JBsp"
                    
                    params = {
                        "member_id": member_id,
                        "token": token,
                        "disuse_enable": "true",
                        "time_zone": "UTC",
                        "disuse_time": disuse_time
                    }
                    qs = urllib.parse.urlencode(params)
                    edit_url = f"https://app.dicloak.com/gin/v1/api/member/open/edit?{qs}"
                    
                    resp = httpx.get(edit_url, timeout=10.0)
                    resp_data = resp.json()
                    
                    if resp_data.get("code") not in (0, 200):
                        log.error("Erreur API Dicloak /edit: %s", resp_data)
                except Exception as e:
                    log.error("Erreur HTTP Dicloak /edit: %s", e)

            _save(data)
            return True
        return False
