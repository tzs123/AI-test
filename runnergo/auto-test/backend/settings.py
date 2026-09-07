"""平台全局配置加载（config/settings.yaml）。"""
import base64
import hashlib
import hmac
import os
import socket
import struct
import time
import yaml
from urllib.parse import quote, urlsplit, urlunsplit


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(_ROOT, "config", "settings.yaml")


def _load() -> dict:
    with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _detect_host_ip() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["ifconfig", "en0"],
            capture_output=True, text=True
        )
        for line in result.stdout.split("\n"):
            if "inet " in line and "127.0.0.1" not in line:
                ip = line.strip().split()[1]
                if ip and ip.count(".") == 3:
                    return ip
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


SETTINGS = _load()

# 路径快捷访问
ROOT = _ROOT
RESULTS_DIR = os.path.join(ROOT, SETTINGS["executor"]["results_dir"])
REPORT_DIR = os.path.join(ROOT, SETTINGS["executor"]["report_dir"])
SCREENSHOTS_DIR = os.path.join(ROOT, SETTINGS["executor"]["screenshots_dir"])
LOGS_DIR = os.path.join(ROOT, SETTINGS["executor"]["logs_dir"])
RUNTIME_DIR = os.path.join(ROOT, SETTINGS["executor"]["runtime_dir"])
DB_PATH = os.path.join(RUNTIME_DIR, "platform.db")
JOBS_FILE = os.path.join(RUNTIME_DIR, "jobs.json")
IMPORTS_DIR = os.path.realpath(os.environ.get("AUTO_TEST_IMPORTS_DIR", os.path.join(ROOT, "imports")))

AUTH_REQUIRED = _env_bool("AUTO_TEST_AUTH_REQUIRED", True)
ALLOW_PYTHON_EDIT = _env_bool("AUTO_TEST_ALLOW_PYTHON_EDIT", False)
PERMISSION_USER_URL = os.environ.get(
    "RUNNERGO_PERMISSION_USER_URL",
    "http://permission:30000/permission/api/v1/user/get",
).strip()
RUNNERGO_MANAGEMENT_API_URL = os.environ.get(
    "RUNNERGO_MANAGEMENT_API_URL",
    "http://manage:30000/management/api/v1",
).strip().rstrip("/")
RUNNERGO_MYSQL_HOST = os.environ.get("RUNNERGO_MYSQL_HOST", "").strip()
RUNNERGO_MYSQL_PORT = int(os.environ.get("RUNNERGO_MYSQL_PORT", "3306") or 3306)
RUNNERGO_MYSQL_USER = os.environ.get("RUNNERGO_MYSQL_USER", "root").strip()
RUNNERGO_MYSQL_PASSWORD = os.environ.get("RUNNERGO_MYSQL_PASSWORD", "")
RUNNERGO_MYSQL_DATABASE = os.environ.get("RUNNERGO_MYSQL_DATABASE", "runnergo").strip()
AUTH_CACHE_TTL_SECONDS = max(1, int(os.environ.get("AUTO_TEST_AUTH_CACHE_TTL_SECONDS", "30")))
AUTH_NEGATIVE_CACHE_TTL_SECONDS = max(
    1, int(os.environ.get("AUTO_TEST_AUTH_NEGATIVE_CACHE_TTL_SECONDS", "5"))
)
CORS_ALLOWED_ORIGINS = _env_csv("AUTO_TEST_CORS_ALLOWED_ORIGINS")

ALLOW_PRIVATE_URLS = _env_bool("AUTO_TEST_ALLOW_PRIVATE_URLS", False)
ALLOWED_URL_HOSTS = _env_csv("AUTO_TEST_ALLOWED_URL_HOSTS")
ALLOWED_URL_CIDRS = _env_csv("AUTO_TEST_ALLOWED_URL_CIDRS")

# Worker 自动启动配置：容器启动时自动拉起 worker 进程
WORKER_AUTO_START = _env_bool("AUTO_TEST_WORKER_AUTO_START", True)
WORKER_COUNT = max(1, int(os.environ.get("AUTO_TEST_WORKER_COUNT", "1") or 1))

TEST_DATA_CENTER_API_URL = os.environ.get(
    "TEST_DATA_CENTER_API_URL",
    "http://testhub-backend:8000",
).strip().rstrip("/")
TEST_DATA_CENTER_AGENT_TOKEN = os.environ.get(
    "TEST_DATA_CENTER_AGENT_TOKEN",
    "runnergo-local-agent-token",
).strip()
REDIS_CFG = SETTINGS.get("redis", {})
SERVER_CFG = SETTINGS.get("server", {})
ALLURE_CLI = os.environ.get("ALLURE_CLI") or SETTINGS.get("allure", {}).get("cli", "allure")

DEFAULT_EXTERNAL_URL = "http://localhost:9999"

_env_external_url = os.environ.get("EXTERNAL_URL", "").rstrip("/")
_config_external_url = SERVER_CFG.get("external_url", "").rstrip("/")
if _env_external_url:
    EXTERNAL_URL = _env_external_url
elif _config_external_url:
    EXTERNAL_URL = _config_external_url
else:
    EXTERNAL_URL = DEFAULT_EXTERNAL_URL
LOCAL_REPORT_HOST = EXTERNAL_URL


def normalize_report_url(url: str) -> str:
    """Return an absolute localhost URL for report links."""
    if not url:
        return ""

    local = urlsplit(LOCAL_REPORT_HOST)
    value = str(url).strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlsplit(value)
        path = parsed.path or "/"
        if path.startswith("/auto-test/report/"):
            path = path.removeprefix("/auto-test")
        return urlunsplit((local.scheme, local.netloc, path, parsed.query, parsed.fragment))

    if not value.startswith("/"):
        value = "/" + value
    if value.startswith("/auto-test/report/"):
        value = value.removeprefix("/auto-test")
    return f"{LOCAL_REPORT_HOST}{value}"


def report_url_for_task(task_id: str) -> str:
    return normalize_report_url(f"/report/tasks/{task_id}/report/")


# ===== Public report URL signing (matches Django TimestampSigner concept) =====
PUBLIC_REPORT_SALT = "ui-automation-public-report"
PUBLIC_REPORT_MAX_AGE_SECONDS = 3650 * 24 * 60 * 60  # 10 years, same as app automation

# Shared secret for HMAC; prefer dedicated env var, fall back to agent token (both stable per deploy)
_PUBLIC_REPORT_SECRET = (
    os.environ.get("UI_PUBLIC_REPORT_SECRET", "").strip()
    or TEST_DATA_CENTER_AGENT_TOKEN
    or "runnergo-ui-report-secret"
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def sign_report_token(task_id: str) -> str:
    """Generate an HMAC-signed timestamped token for a public report URL.

    Format: <task_id>:<b64ts>:<b64hmac>  (URL-safe, no padding)
    The hmac covers salt+task_id+timestamp to guarantee tamper-proofness.
    """
    ts = int(time.time())
    ts_bytes = struct.pack(">Q", ts)
    msg = f"{PUBLIC_REPORT_SALT}:{task_id}:{ts}".encode("utf-8")
    digest = hmac.new(_PUBLIC_REPORT_SECRET.encode("utf-8"), msg, hashlib.sha256).digest()
    return f"{task_id}:{_b64url_encode(ts_bytes)}:{_b64url_encode(digest)}"


def unsign_report_token(token: str, max_age: int = PUBLIC_REPORT_MAX_AGE_SECONDS) -> str:
    """Validate a signed report token. Returns the task_id if valid, else raises ValueError."""
    if not token or token.count(":") != 2:
        raise ValueError("bad token format")
    task_id, ts_b64, sig_b64 = token.split(":", 2)
    try:
        ts_bytes = _b64url_decode(ts_b64)
        provided_sig = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"bad token encoding: {exc}") from exc
    if len(ts_bytes) != 8:
        raise ValueError("bad token: timestamp truncated")
    ts = struct.unpack(">Q", ts_bytes)[0]

    # Recompute expected signature
    msg = f"{PUBLIC_REPORT_SALT}:{task_id}:{ts}".encode("utf-8")
    expected_sig = hmac.new(
        _PUBLIC_REPORT_SECRET.encode("utf-8"), msg, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise ValueError("bad token: signature mismatch")
    if max_age and int(time.time()) - ts > max_age:
        raise ValueError("bad token: expired")
    return task_id


def public_report_path_for_task(task_id: str, file_path: str = "") -> str:
    token = sign_report_token(task_id)
    safe_token = quote(token, safe="")
    base = f"/public-report/{task_id}/{safe_token}/report/"
    if file_path:
        # file_path is relative to the task report dir; join safely
        rel = file_path.lstrip("/").replace("\\", "/")
        if rel and ".." not in rel.split("/"):
            base += rel
    return base


def public_report_url_for_task(task_id: str) -> str:
    """Return the full external URL for the public (no-login) Allure report of a task."""
    return normalize_report_url(public_report_path_for_task(task_id))


def ensure_dirs():
    for d in (
        RESULTS_DIR,
        REPORT_DIR,
        SCREENSHOTS_DIR,
        LOGS_DIR,
        RUNTIME_DIR,
        IMPORTS_DIR,
    ):
        os.makedirs(d, exist_ok=True)
