"""WebUI 密码认证与登录会话（纯标准库）。

设计
----
- 密码：``hashlib.scrypt`` + 随机盐，认证文件只存 salt/hash 的 hex；
  不存明文、不回显、不入日志。
- 认证数据：``<data_dir>/webui_auth.json``，原子写（tmp + os.replace）、权限 0600。
- 状态：
    * ``unset``  尚无认证文件 → 允许一次性设置密码；
    * ``ready``  有效认证文件 → 只能登录；
    * ``locked`` 文件存在但解析失败/格式非法/算法不支持 → 拒绝设置与登录，
      只能在本机删除文件或运行 ``--reset-webui-password`` 重置，
      避免坏数据被“回落首设”从而被接管。
- 会话：内存中的随机 token（``secrets.token_urlsafe``）+ 到期时间；
  Cookie 属性由 webui.py 设置（HttpOnly / SameSite=Strict）。
- 限速：按来源 IP 记录失败次数，超过阈值后指数锁定。

本模块不含任何 HTTP 逻辑，便于单测。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

AUTH_FILE_NAME = "webui_auth.json"
AUTH_VERSION = 1
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 256

SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 1024
RATE_MAX_FAILURES = 5
RATE_LOCK_BASE_SECONDS = 30
RATE_LOCK_MAX_SECONDS = 15 * 60
# 指数在截断后计算，防止长期失败使 2**count 无限增大；失败计数本身也截断保存。
RATE_LOCK_MAX_EXP = 20
MAX_RATE_ENTRIES = 1024

STATUS_UNSET = "unset"
STATUS_READY = "ready"
STATUS_LOCKED = "locked"


def _hash_password(password: str, salt: bytes) -> bytes:
    """scrypt 派生（随机盐由调用方生成）。"""
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=DK_LEN,
    )


class AuthManager:
    """认证状态 + 会话 + 限速的唯一持有者（线程安全）。"""

    def __init__(self, data_dir: Path, log_fn=None):
        self._lock = threading.RLock()
        self._log = log_fn if log_fn is not None else (lambda _m: None)
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / AUTH_FILE_NAME
        self._record: dict | None = None
        self.status = STATUS_UNSET
        self._sessions: dict[str, float] = {}
        # ip -> (连续失败次数, 锁定截止时间)
        self._failures: dict[str, tuple[int, float]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # 载入 / 持久化
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._record = None
                self.status = STATUS_UNSET
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                self._log("认证数据解析失败，已锁定（需本机重置）：%s" % e)
                self._record = None
                self.status = STATUS_LOCKED
                return
            if not self._valid_record(raw):
                self._log("认证数据格式非法，已锁定（需本机重置）。")
                self._record = None
                self.status = STATUS_LOCKED
                return
            self._record = raw
            self.status = STATUS_READY

    @staticmethod
    def _valid_record(raw: object) -> bool:
        if not isinstance(raw, dict):
            return False
        if raw.get("version") != AUTH_VERSION or raw.get("algo") != "scrypt":
            return False
        salt = raw.get("salt")
        digest = raw.get("hash")
        if not isinstance(salt, str) or not isinstance(digest, str):
            return False
        try:
            bytes.fromhex(salt)
            bytes.fromhex(digest)
        except ValueError:
            return False
        # KDF 参数白名单：只接受本程序固定参数，防止损坏文件触发资源爆炸
        if (raw.get("n"), raw.get("r"), raw.get("p")) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        return True

    def _claim_and_persist(self, record: dict) -> None:
        """跨进程排他地创建认证文件：先写 0600 tmp，再 os.link 到最终名。

        ``os.link`` 在目标已存在时抛 FileExistsError，等价于跨进程的一次性
        创建（比 os.replace 的“覆盖”语义更安全）。写入完成后文件权限 600。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("%s.%d.tmp" % (self.path.name, os.getpid()))
        payload = json.dumps(record, ensure_ascii=False, indent=2)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            try:
                os.link(str(tmp), str(self.path))
            except FileExistsError:
                raise
            except OSError:
                # 个别文件系统不支持硬链接：退化为存在性检查 + replace
                if self.path.exists():
                    raise FileExistsError(str(self.path))
                os.replace(str(tmp), str(self.path))
        finally:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        try:
            dir_fd = os.open(str(self.data_dir), os.O_RDONLY)
            os.fsync(dir_fd)
            os.close(dir_fd)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    def needs_setup(self) -> bool:
        with self._lock:
            return self.status == STATUS_UNSET

    def is_locked(self) -> bool:
        with self._lock:
            return self.status == STATUS_LOCKED

    def is_ready(self) -> bool:
        with self._lock:
            return self.status == STATUS_READY

    # ------------------------------------------------------------------ #
    # 首次设置（一次性、并发安全、落盘成功才激活）
    # ------------------------------------------------------------------ #
    def setup(self, password: str) -> tuple[bool, str]:
        if not isinstance(password, str) or not (
            MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN
        ):
            return False, "密码长度需在 %d-%d 位之间。" % (
                MIN_PASSWORD_LEN,
                MAX_PASSWORD_LEN,
            )
        with self._lock:
            if self.status == STATUS_READY:
                return False, "密码已设置，请直接登录。"
            if self.status == STATUS_LOCKED:
                return False, "认证数据损坏，已锁定；请在本机重置后重新设置。"
        # KDF 计算放在锁外，避免阻塞已登录会话的校验等健康请求
        salt = os.urandom(16)
        digest = _hash_password(password, salt)
        record = {
            "version": AUTH_VERSION,
            "algo": "scrypt",
            "salt": salt.hex(),
            "hash": digest.hex(),
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "created_at": int(time.time()),
        }
        with self._lock:
            if self.status != STATUS_UNSET:  # 期间已被本进程其它请求设置
                return False, "密码已设置，请直接登录。"
            try:
                self._claim_and_persist(record)
            except FileExistsError:
                # 其它进程/实例已抢先创建：重新载入后按已设置处理
                self._load()
                return False, "密码已设置，请直接登录。"
            except Exception as e:
                self._log("认证数据写入失败（未激活）：%s" % e)
                return False, "认证数据写入失败，请检查 data 目录权限后重试。"
            self._record = record
            self.status = STATUS_READY
            return True, ""

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def verify(self, password: str) -> bool:
        if not isinstance(password, str):
            return False
        with self._lock:
            if self.status != STATUS_READY or not self._record:
                return False
            salt_hex = self._record.get("salt")
            hash_hex = self._record.get("hash")
        # KDF 在锁外计算：不长时间占用全局锁
        try:
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(_hash_password(password, salt), expected)

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_sessions_locked()
            if len(self._sessions) >= MAX_SESSIONS:  # 有界：淘汰最早到期者
                oldest = min(self._sessions.items(), key=lambda kv: kv[1])[0]
                self._sessions.pop(oldest, None)
            self._sessions[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def validate_session(self, token: object) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires < time.time():
                self._sessions.pop(token, None)
                return False
            return True

    def revoke_session(self, token: object) -> None:
        if not isinstance(token, str):
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _prune_sessions_locked(self) -> None:
        now = time.time()
        for token in [t for t, exp in self._sessions.items() if exp < now]:
            self._sessions.pop(token, None)

    @property
    def session_ttl_seconds(self) -> int:
        return SESSION_TTL_SECONDS

    # ------------------------------------------------------------------ #
    # 失败限速
    # ------------------------------------------------------------------ #
    def is_rate_limited(self, ip: str) -> bool:
        with self._lock:
            _count, locked_until = self._failures.get(ip, (0, 0.0))
            return time.time() < locked_until

    def record_failure(self, ip: str) -> None:
        with self._lock:
            if ip not in self._failures and len(self._failures) >= MAX_RATE_ENTRIES:
                oldest = min(self._failures.items(), key=lambda kv: kv[1][1])[0]
                self._failures.pop(oldest, None)
            count, _until = self._failures.get(ip, (0, 0.0))
            count += 1
            count = min(count, RATE_MAX_FAILURES + RATE_LOCK_MAX_EXP)
            delay = 0.0
            if count >= RATE_MAX_FAILURES:
                # 先截断指数再乘，避免 2**count 无限增大
                exponent = min(count - RATE_MAX_FAILURES, RATE_LOCK_MAX_EXP)
                delay = min(
                    RATE_LOCK_BASE_SECONDS * (2**exponent),
                    RATE_LOCK_MAX_SECONDS,
                )
            self._failures[ip] = (count, time.time() + delay)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    # ------------------------------------------------------------------ #
    # 本机显式重置（仅 CLI 调用，不暴露 HTTP）
    # ------------------------------------------------------------------ #
    def reset(self) -> bool:
        with self._lock:
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError as e:
                self._log("重置认证数据失败：%s" % e)
                return False
            self._record = None
            self.status = STATUS_UNSET
            self._sessions.clear()
            self._failures.clear()
            return True


def resolve_data_dir(config_path: str | None, data_dir: str | None) -> Path:
    """解析认证数据目录：绝对路径原样；相对路径以配置文件所在目录为基准。

    Docker 场景下把相对路径指向可写卷（如 ./data:/app/data）即可在重建/重启后保留。
    """
    raw = (data_dir or "data").strip() or "data"
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    base = Path(config_path).resolve().parent if config_path else Path.cwd()
    return (base / candidate).resolve()
