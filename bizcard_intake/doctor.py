"""Environment checks shared by `bizcard doctor`, `bizcard setup` and the bot's health alert."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.request import urlopen

from . import parser
from .contacts import CONTACTS_SCOPE, LOGIN_COMMAND, ContactSaveError, gws_auth_status, run_gws
from .image_processing import _ensure_vision_rectifier
from .vision_ocr import _ensure_vision_ocr


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def check_python() -> Check:
    missing = []
    for module in ("numpy", "PIL"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    if missing:
        return Check("python", False, f"모듈 없음: {', '.join(missing)}", "pip install -r requirements.txt  (또는 BIZCARD_PYTHON으로 다른 인터프리터 지정)")
    try:
        __import__("cv2")
    except Exception:
        return Check("python", True, "numpy, PIL 사용 가능 (cv2 없음: Apple Vision 실패 시 OpenCV 보정 폴백은 건너뜀)")
    return Check("python", True, "numpy, PIL, cv2 사용 가능")


def check_vision() -> Check:
    if shutil.which("swiftc") is None:
        return Check("vision", False, "swiftc 없음: Apple Vision 보정/OCR 사용 불가 (OpenCV 보정만 동작)", "xcode-select --install")
    rectifier = _ensure_vision_rectifier()
    ocr = _ensure_vision_ocr()
    if rectifier is None or ocr is None:
        return Check("vision", False, "Vision 도우미 빌드 실패", "swiftc scripts/vision_ocr.swift -O -o .cache/vision_ocr 를 직접 실행해 오류 확인")
    return Check("vision", True, "Apple Vision 도우미 준비됨")


def check_gws() -> Check:
    if shutil.which("gws") is None:
        return Check("gws", False, "gws CLI 없음", "brew install googleworkspace-cli  (또는 npm install -g @googleworkspace/cli)")
    status = gws_auth_status()
    if not status.get("token_valid"):
        error = status.get("token_error") or "로그인 필요"
        return Check("gws", False, f"Google 인증 무효: {error}", LOGIN_COMMAND)
    scopes = status.get("scopes") or []
    if CONTACTS_SCOPE not in scopes:
        return Check("gws", False, "토큰에 contacts 권한 없음", LOGIN_COMMAND)
    # auth status only reads the stored token; a real read catches vanished
    # credentials ("No credentials provided") and revoked grants.
    params = {"resourceName": "people/me", "pageSize": 1, "personFields": "names"}
    try:
        run_gws(["people", "people", "connections", "list", "--params", json.dumps(params)])
    except ContactSaveError as exc:
        fix = "잠시 후 bizcard doctor 재실행" if exc.kind == "transient" else LOGIN_COMMAND
        return Check("gws", False, f"Google Contacts 읽기 실패: {exc}", fix)
    return Check("gws", True, f"로그인됨: {status.get('user', '?')}")


def check_parser() -> Check:
    backend = parser.backend_name()
    if backend == "openai":
        source = parser.openai_key_source()
        if not source:
            return Check("parser", False, "OpenAI API 키 없음", ".env에 OPENAI_API_KEY=... 저장 또는 BIZCARD_PARSER=codex")
        fallback = parser.fallback_backend("openai") or "없음"
        model = os.environ.get("BIZCARD_OPENAI_MODEL", parser.DEFAULT_OPENAI_MODEL)
        effort = os.environ.get("BIZCARD_REASONING_EFFORT", parser.DEFAULT_REASONING_EFFORT)
        cross = f" + 교차검증 {os.environ.get('BIZCARD_ANTHROPIC_MODEL', parser.DEFAULT_ANTHROPIC_MODEL)}" if parser.crosscheck_enabled() else ""
        return Check("parser", True, f"OpenAI API {model}{cross} (추론 {effort}, 키: {source}, 실패 시 {fallback})")
    binary = parser.find_binary(backend)
    if not binary:
        return Check("parser", False, f"{backend} CLI 없음", "codex: npm install -g @openai/codex && codex login   |   claude: npm install -g @anthropic-ai/claude-code && claude login")
    try:
        if backend == "codex":
            completed = subprocess.run([binary, "login", "status"], capture_output=True, text=True, timeout=30)
            logged_in = completed.returncode == 0 and "Logged in" in (completed.stdout + completed.stderr)
        else:
            completed = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=30)
            logged_in = completed.returncode == 0 and json.loads(completed.stdout or "{}").get("loggedIn") is True
    except Exception as exc:
        return Check("parser", False, f"{backend} 상태 확인 실패: {exc}", f"{backend} login")
    if not logged_in:
        return Check("parser", False, f"{backend} 로그인 필요 (ChatGPT Plus 이상 또는 Claude 구독)", f"{backend} login")
    return Check("parser", True, f"{backend} 로그인됨 ({binary})")


def check_telegram() -> Check:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return Check("telegram", False, "TELEGRAM_BOT_TOKEN 없음", ".env에 @BotFather 토큰을 넣으세요")
    if not os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"):
        return Check("telegram", False, "TELEGRAM_ALLOWED_CHAT_IDS 없음 (허용된 채팅만 봇을 쓸 수 있습니다)", "봇에 메시지를 보낸 뒤 https://api.telegram.org/bot<토큰>/getUpdates 의 chat.id 값을 .env에 넣으세요")
    try:
        with urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        username = payload["result"]["username"]
    except Exception as exc:
        return Check("telegram", False, f"봇 토큰 확인 실패: {exc}", "토큰을 다시 확인하세요")
    return Check("telegram", True, f"@{username}")


CHECKS: dict[str, Callable[[], Check]] = {
    "python": check_python,
    "vision": check_vision,
    "gws": check_gws,
    "parser": check_parser,
    "telegram": check_telegram,
}


def run_checks(names: Optional[list[str]] = None) -> list[Check]:
    return [CHECKS[name]() for name in (names or list(CHECKS))]


def format_report(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        lines.append(f"[{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"       해결: {check.fix}")
    return "\n".join(lines)
