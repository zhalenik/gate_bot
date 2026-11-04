"""Administrator authentication with current-role checks and revocable sessions."""
import hashlib
import hmac
import re
from functools import wraps
from typing import Any, Callable, Dict, Optional

from flask import current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from storage.json_store import JsonStore


class AdminAuth:
    """Passwords stay in private state; cookies never define an administrator's role."""

    MIN_PASSWORD_LENGTH = 12
    MAX_PASSWORD_LENGTH = 1024

    def __init__(self, admins_file: str, branch_exists: Optional[Callable] = None):
        self.admins_file = admins_file
        self.store = JsonStore(admins_file)
        self.branch_exists = branch_exists

    def load_admins(self) -> Dict[str, Any]:
        return self.store.read()

    def save_admins(self, admins: Dict[str, Any]) -> None:
        """Compatibility for explicit administrative imports, not read/modify/write."""
        self.store.replace(admins)

    @classmethod
    def validate_new_password(cls, password: Any) -> None:
        if not isinstance(password, str) or not cls.MIN_PASSWORD_LENGTH <= len(password) <= cls.MAX_PASSWORD_LENGTH:
            raise ValueError(f"Пароль должен содержать от {cls.MIN_PASSWORD_LENGTH} до {cls.MAX_PASSWORD_LENGTH} символов")

    @classmethod
    def hash_password(cls, password: str) -> str:
        cls.validate_new_password(password)
        return generate_password_hash(password, method="scrypt")

    @classmethod
    def verify_password(cls, password: Any, hash_value: Any) -> bool:
        if not isinstance(password, str) or not isinstance(hash_value, str) or len(password) > cls.MAX_PASSWORD_LENGTH:
            return False
        # Existing private state can be migrated after one successful login.
        if re.fullmatch(r"[a-fA-F0-9]{64}", hash_value):
            actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return hmac.compare_digest(actual, hash_value.lower())
        if not hash_value.startswith(("scrypt:", "pbkdf2:")):
            return False
        try:
            return check_password_hash(hash_value, password)
        except (ValueError, TypeError):
            return False

    def _scope_is_valid(self, admin: Any) -> bool:
        if not isinstance(admin, dict) or admin.get("disabled", False):
            return False
        if admin.get("is_super") is True:
            return True
        branch_id = admin.get("branch_id")
        if isinstance(branch_id, bool):
            return False
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            return False
        if branch_id < 1:
            return False
        return self.branch_exists is None or bool(self.branch_exists(branch_id))

    def verify_admin(self, username: Any, password: Any) -> bool:
        if not isinstance(username, str) or not username or len(username) > 200:
            return False
        admin = self.load_admins().get(username)
        return self._scope_is_valid(admin) and self.verify_password(password, admin.get("password_hash"))

    def get_admin(self, username: str) -> Optional[Dict[str, Any]]:
        admin = self.load_admins().get(username)
        return admin if isinstance(admin, dict) else None

    @staticmethod
    def _session_fingerprint(admin: Dict[str, Any]) -> str:
        secret = current_app.secret_key
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        # A password change, disabling or reassignment immediately invalidates cookies.
        value = "|".join(str(admin.get(key, "")) for key in (
            "password_hash", "session_version", "branch_id", "is_super", "disabled"
        ))
        return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _set_session(self, username: str, admin: Dict[str, Any]) -> None:
        session.clear()
        session.permanent = True
        session["admin_logged_in"] = True
        session["admin_username"] = username
        session["admin_auth_version"] = self._session_fingerprint(admin)

    def login_admin(self, username: Any, password: Any) -> bool:
        if not isinstance(username, str) or not username or len(username) > 200:
            return False
        with self.store.transaction() as admins:
            admin = admins.get(username)
            if not self._scope_is_valid(admin) or not self.verify_password(password, admin.get("password_hash")):
                return False
            if re.fullmatch(r"[a-fA-F0-9]{64}", admin["password_hash"]):
                # Preserve access for legacy short passwords, but never create new weak ones.
                admin["password_hash"] = generate_password_hash(password, method="scrypt")
            saved = dict(admin)
        self._set_session(username, saved)
        return True

    def change_password(self, username: str, old_password: Any, new_password: Any) -> bool:
        self.validate_new_password(new_password)
        with self.store.transaction() as admins:
            admin = admins.get(username)
            if not self._scope_is_valid(admin) or not self.verify_password(old_password, admin.get("password_hash")):
                return False
            admin["password_hash"] = self.hash_password(new_password)
            saved = dict(admin)
        self._set_session(username, saved)
        return True

    def logout_admin(self) -> None:
        session.clear()

    def get_current_admin(self) -> Optional[Dict[str, Any]]:
        if session.get("admin_logged_in") is not True:
            return None
        username = session.get("admin_username")
        if not isinstance(username, str):
            session.clear()
            return None
        admin = self.get_admin(username)
        if not self._scope_is_valid(admin):
            session.clear()
            return None
        expected = self._session_fingerprint(admin)
        supplied = session.get("admin_auth_version", "")
        if not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
            session.clear()
            return None
        return admin

    def is_logged_in(self) -> bool:
        return self.get_current_admin() is not None

    @staticmethod
    def _unauthorized():
        if request.path.startswith("/admin/api/"):
            return jsonify(status="error", message="Требуется вход в систему"), 401
        return redirect(url_for("admin_login"))

    def require_auth(self, function):
        @wraps(function)
        def decorated(*args, **kwargs):
            if not self.is_logged_in():
                return self._unauthorized()
            return function(*args, **kwargs)
        return decorated

    def require_super_admin(self, function):
        @wraps(function)
        def decorated(*args, **kwargs):
            admin = self.get_current_admin()
            if admin is None:
                return self._unauthorized()
            if admin.get("is_super") is not True:
                return jsonify(status="error", message="Требуются права суперадминистратора"), 403
            return function(*args, **kwargs)
        return decorated
