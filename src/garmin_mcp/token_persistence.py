"""Container token persistence. Never log credentials, payloads or exceptions."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse

REDIS_KEY = "garmin:mcp:tokens"
MAX_BYTES = 1024 * 1024
# Compare and write in one Redis operation; a previous deployment cannot
# replace a value that another deployment has already refreshed.
CAS_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[2] then return 1 end
if current ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""

class PersistenceConflict(Exception):
    pass

class PersistenceError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def configured():
    url = os.getenv("UPSTASH_REDIS_REST_URL", "")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    if not url and not token:
        return False
    parsed = urllib.parse.urlsplit(url)
    if (not token or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise PersistenceError("Invalid persistence configuration")
    return True

def _upstash_request(command):
    if not configured():
        return None
    try:
        request = urllib.request.Request(
            os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/"),
            data=json.dumps(command).encode("utf-8"),
            headers={"Authorization": "Bearer " + os.environ["UPSTASH_REDIS_REST_TOKEN"],
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        result = json.loads(raw)
        if not isinstance(result, dict) or "error" in result or "result" not in result:
            raise ValueError
        return result
    except (OSError, ValueError):
        raise PersistenceError("Persistence request failed") from None

def valid_payload(data):
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_BYTES:
        raise PersistenceError("Invalid token data")
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or not all(
            isinstance(value.get(key), str) and value[key].strip()
            for key in ("di_token", "di_refresh_token", "di_client_id")
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise PersistenceError("Invalid token data") from None
    return data

def read_file(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    return valid_payload(data.decode("utf-8"))

def write_tokens(token_dir, data):
    valid_payload(data)
    directory = Path(token_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    fd, name = tempfile.mkstemp(dir=directory, prefix=".tokens-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.chmod(name, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / "garmin_tokens.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)

def restore_tokens(token_dir):
    result = _upstash_request(["GET", os.getenv("UPSTASH_REDIS_KEY", REDIS_KEY)])
    if result is None or result["result"] is None:
        return False
    write_tokens(token_dir, valid_payload(result["result"]))
    return True

def backup_tokens(token_dir, data=None, *, expected):
    data = read_file(Path(token_dir) / "garmin_tokens.json") if data is None else valid_payload(data)
    valid_payload(expected)
    result = _upstash_request([
        "EVAL", CAS_SCRIPT, "1", os.getenv("UPSTASH_REDIS_KEY", REDIS_KEY), expected, data])
    if result is None:
        return False
    if result["result"] == 0:
        raise PersistenceConflict("Remote tokens changed; restart required")
    if result["result"] != 1:
        raise PersistenceError("Persistence write failed")
    return True


def initialize_remote(source):
    """Explicit one-time import of a known-current file; never overwrite a key."""
    if not configured():
        raise PersistenceError("Persistence is not configured")
    data = read_file(source)
    result = _upstash_request([
        "SET", os.getenv("UPSTASH_REDIS_KEY", REDIS_KEY), data, "NX"])
    if result["result"] != "OK":
        raise PersistenceError("Initialization refused; remote key may already exist")


def bootstrap(token_dir):
    # Never replace a possibly newer remote token with a seed after a failed GET.
    enabled = configured()
    if enabled:
        if restore_tokens(token_dir):
            return True
        # A Render secret file may be obsolete. Never seed implicitly, even
        # when Redis is empty or the local directory already contains tokens.
        raise PersistenceError("Remote tokens missing; explicit initialization required")
    path = Path(token_dir) / "garmin_tokens.json"
    if not path.exists():
        seed = Path(os.getenv("GARMIN_TOKEN_SEED", "/etc/secrets/garmin_tokens.json"))
        if seed.exists():
            write_tokens(token_dir, read_file(seed))
    if path.exists():
        read_file(path)
        path.chmod(0o600)
    return enabled

def main():
    os.umask(0o077)
    token_dir = os.path.expandvars(os.path.expanduser(os.getenv("GARMINTOKENS", "~/.garminconnect")))
    os.environ["GARMINTOKENS"] = token_dir
    try:
        enabled = bootstrap(token_dir)
    except (PersistenceError, OSError, ValueError):
        print("Token bootstrap failed; check persistence configuration and availability.", file=sys.stderr)
        return 1
    # Capture the restored version BEFORE the child can refresh the file.
    expected = read_file(Path(token_dir) / "garmin_tokens.json") if enabled else None
    child = None
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = subprocess.Popen(sys.argv[1:] or ["garmin-mcp"])
    if stopping:
        child.terminate()
    warned = False
    def sync():
        nonlocal expected, warned
        if not enabled:
            return
        try:
            data = read_file(Path(token_dir) / "garmin_tokens.json")
            if data != expected and backup_tokens(token_dir, data, expected=expected):
                expected = data
            warned = False
        except PersistenceConflict:
            print("Remote tokens changed; stopping this writer. Restart to restore.", file=sys.stderr)
            stop(signal.SIGTERM, None)
        except (PersistenceError, OSError, ValueError):
            if not warned:
                print("Token sync unavailable; will retry without discarding local tokens.", file=sys.stderr)
                warned = True
    while child.poll() is None:
        sync()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if stopping:
                child.kill()
                child.wait()
    sync()
    return child.returncode if child.returncode >= 0 else 128 - child.returncode

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--initialize-upstash":
        try:
            if len(sys.argv) != 3:
                raise PersistenceError("Expected a source file")
            initialize_remote(sys.argv[2])
        except (PersistenceError, OSError, ValueError):
            print("Initialization failed or refused; no overwrite attempted.", file=sys.stderr)
            sys.exit(1)
        print("Remote tokens initialized without overwriting an existing key.")
    else:
        sys.exit(main())
