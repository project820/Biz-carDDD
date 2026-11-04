"""Business-card parsing: OpenAI API (default when a key exists), codex or claude CLI.

- openai: Responses API with gpt-6-luna, reasoning effort medium and a strict
  JSON schema (card_schema.json). Uses the OpenAI API key already stored for the
  Decisions API. Billed per token on the API account.
- codex / claude: subscription CLIs driven headless; also the fallback when an
  API call fails, so a network or quota problem never blocks a card.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import base64
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .draft import CardDraft
from .vision_ocr import OcrResult

SCHEMA_PATH = Path(__file__).resolve().parent / "card_schema.json"
DEFAULT_CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_CLAUDE_MODEL = "haiku"
DEFAULT_OPENAI_MODEL = "gpt-6-luna"
DEFAULT_REASONING_EFFORT = "medium"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
# Same lookup order as the decision skill: process env, then key files.
OPENAI_KEY_FILES = ("~/.config/openai/credentials.env", "~/.config/gb-invest/decisions.env")
EXTRA_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

log = logging.getLogger("bizcard")

FIELD_GUIDE = (
    "JSON 필드: name(사람 이름만, 직함 제외), title(명함에 적힌 직함 원문 그대로, 없으면 빈 문자열), "
    "display_title(전화 수신 화면에 붙일 짧은 직함 하나. 예: '영업팀장/컨설턴트 (Sales Executive)' -> '영업팀장', "
    "'대표이사 사장' -> '대표이사', 명함에 직함이 없으면 빈 문자열), company(회사명, 명함 표기 그대로), "
    "mobile, work, fax, email, website, address(각각 문자열 배열), "
    "notes(인증서·회사 특징·자격증 등 부수 정보), warnings(읽기 어렵거나 누락된 항목과 사유, 문자열 배열).\n"
    "규칙: 추측하지 말고 확실하지 않은 값은 비우고 warnings에 사유를 적어라. 명함에 없는 직함을 만들어 넣지 마라. "
    "휴대전화/회사전화/팩스를 적극 구분해라. '0126~7'처럼 범위로 적힌 번호는 두 번호로 풀어 써라. "
    "배경 명함·손가락·테이블·반사광에서 온 텍스트는 무시해라.\n"
    "다른 말 없이 JSON 객체 하나만 출력해라."
)


def backend_name() -> str:
    choice = os.environ.get("BIZCARD_PARSER", "auto").strip().lower()
    if choice in ("openai", "codex", "claude"):
        return choice
    if openai_api_key():
        return "openai"
    return "codex" if find_binary("codex") else "claude"


def fallback_backend(primary: str) -> Optional[str]:
    """CLI used when the primary backend fails (only for the API backend)."""
    if primary != "openai":
        return None
    for name in ("codex", "claude"):
        if find_binary(name):
            return name
    return None


def openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    files = [os.environ["BIZCARD_OPENAI_ENV_FILE"]] if os.environ.get("BIZCARD_OPENAI_ENV_FILE") else list(OPENAI_KEY_FILES)
    for name in files:
        path = Path(name).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            match = re.match(r"\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(.*)$", line)
            if match:
                value = match.group(1).strip().strip("'").strip('"')
                if value:
                    return value
    return ""


def openai_key_source() -> str:
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "OPENAI_API_KEY"
    files = [os.environ["BIZCARD_OPENAI_ENV_FILE"]] if os.environ.get("BIZCARD_OPENAI_ENV_FILE") else list(OPENAI_KEY_FILES)
    for name in files:
        path = Path(name).expanduser()
        if path.is_file() and "OPENAI_API_KEY" in path.read_text(encoding="utf-8", errors="ignore"):
            return str(path)
    return ""


def find_binary(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for directory in EXTRA_BIN_DIRS:
        candidate = Path(directory).expanduser() / name
        if candidate.exists():
            return str(candidate)
    return None


def parse_card(image_path: Path, ocr: OcrResult, side: str) -> CardDraft:
    side_label = "앞면" if side == "front" else "뒷면"
    raw_ocr = ocr.text[:5000] if ocr.text else "(OCR 원문 없음)"
    prompt = (
        f"명함 {side_label}의 보정 이미지와 Apple Vision OCR 원문을 대조해 Google Contacts 입력 필드를 채워라. "
        "이름과 휴대전화가 가장 중요하고 회사명과 이메일은 보조 필드다.\n\n"
        f"{FIELD_GUIDE}\n\n"
        f"[OCR 평균 confidence: {ocr.average_confidence:.3f}]\n[OCR 원문]\n{raw_ocr}"
    )
    data = run_backend(prompt, image_path=image_path)
    if data is None:
        return CardDraft(warnings=["자동 파싱 실패: 직접 입력하거나 재시도하세요"])
    return CardDraft.from_dict(data)


def merge_drafts(front: CardDraft, back: CardDraft) -> CardDraft:
    prompt = (
        "앞면/뒷면 명함 초안(JSON)을 병합해 중복 없는 하나의 JSON을 만들어라. 뒷면은 추가 정보만 반영한다.\n\n"
        f"{FIELD_GUIDE}\n\n[앞면]\n{front.to_json()}\n\n[뒷면]\n{back.to_json()}"
    )
    data = run_backend(prompt)
    if data is not None:
        return CardDraft.from_dict(data)
    merged = CardDraft.from_dict(front.to_dict())
    for key in ("mobile", "work", "fax", "email", "website", "address"):
        setattr(merged, key, getattr(merged, key) + [v for v in getattr(back, key) if v not in getattr(merged, key)])
    if back.notes:
        merged.notes = f"{merged.notes}\n{back.notes}".strip()
    merged.warnings.append("뒷면 병합 실패: 뒷면 정보를 단순 추가함")
    return merged.normalize()


def revise_draft(draft: CardDraft, reply: str) -> Optional[CardDraft]:
    prompt = (
        "아래 명함 초안(JSON)을 승인자의 답변에 따라 수정해라. "
        "답변에서 언급하지 않은 항목은 그대로 유지하고, 답변으로 해소된 warnings 항목은 지워라. locked 필드는 그대로 둬라.\n\n"
        f"{FIELD_GUIDE}\n\n[초안]\n{draft.to_json()}\n\n[승인자 답변]\n{reply}"
    )
    data = run_backend(prompt)
    return CardDraft.from_dict(data) if data is not None else None


def run_backend(prompt: str, image_path: Optional[Path] = None) -> Optional[dict]:
    primary = backend_name()
    for backend in [primary] + [b for b in [fallback_backend(primary)] if b]:
        try:
            if backend == "openai":
                return run_openai(prompt, image_path)
            output = run_codex(prompt, image_path) if backend == "codex" else run_claude(prompt, image_path)
        except Exception as exc:
            log.warning("%s parser failed: %s", backend, exc)
            continue
        data = extract_json(output)
        if data is not None:
            return data
        log.warning("%s parser returned no JSON: %s", backend, (output or "")[-300:])
    return None


def openai_request_body(prompt: str, image_path: Optional[Path]) -> dict:
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    if image_path is not None:
        suffix = Path(image_path).suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
        encoded = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {
        "model": os.environ.get("BIZCARD_OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        "reasoning": {"effort": os.environ.get("BIZCARD_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)},
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_schema", "name": "business_card", "schema": schema, "strict": True}},
        "store": False,
    }


def run_openai(prompt: str, image_path: Optional[Path]) -> dict:
    key = openai_api_key()
    if not key:
        raise RuntimeError("OpenAI API key not found")
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(openai_request_body(prompt, image_path)).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=parser_timeout()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error", {}).get("message") or body
        except (json.JSONDecodeError, AttributeError):
            message = body
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {message[:300]}") from exc
    usage = result.get("usage") or {}
    log.info(
        "openai model=%s tokens in=%s out=%s reasoning=%s",
        result.get("model"), usage.get("input_tokens"), usage.get("output_tokens"),
        (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
    )
    if result.get("status") not in (None, "completed"):
        raise RuntimeError(f"OpenAI response status={result.get('status')}")
    texts = [
        part.get("text", "")
        for item in result.get("output", [])
        for part in (item.get("content") or [])
        if part.get("type") == "output_text"
    ]
    refusals = [part for item in result.get("output", []) for part in (item.get("content") or []) if part.get("type") == "refusal"]
    if refusals:
        raise RuntimeError("OpenAI refused the request")
    data = extract_json("".join(texts))
    if data is None:
        raise RuntimeError("OpenAI returned no JSON")
    return data


def codex_command(prompt_via_stdin: bool, image_path: Optional[Path], workdir: str) -> list[str]:
    binary = find_binary("codex")
    if not binary:
        raise FileNotFoundError("codex CLI not found")
    command = [
        binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
        "-C", workdir, "--ignore-user-config", "--ignore-rules", "--ephemeral",
        "-m", os.environ.get("BIZCARD_CODEX_MODEL", DEFAULT_CODEX_MODEL),
        "-c", 'model_reasoning_effort="low"',
    ]
    if image_path is not None:
        command += ["--image", str(image_path)]
    if prompt_via_stdin:
        command.append("-")
    return command


def run_codex(prompt: str, image_path: Optional[Path]) -> str:
    # Empty working directory + ignored user config/rules: no AGENTS.md, skills or
    # project context is loaded, which halves the tokens per call.
    with tempfile.TemporaryDirectory(prefix="bizcard-codex-") as workdir:
        result = subprocess.run(
            codex_command(True, image_path, workdir),
            input=prompt, capture_output=True, text=True, timeout=parser_timeout(),
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:] or f"codex exit={result.returncode}")
    tokens = re.search(r"tokens used\s*\n\s*([\d,]+)", result.stderr)
    if tokens:
        log.info("codex tokens used=%s", tokens.group(1))
    return result.stdout


def claude_command(image_path: Optional[Path]) -> list[str]:
    binary = find_binary("claude")
    if not binary:
        raise FileNotFoundError("claude CLI not found")
    command = [
        binary, "-p", "--model", os.environ.get("BIZCARD_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
        "--no-session-persistence", "--max-turns", "3",
        "--json-schema", SCHEMA_PATH.read_text(encoding="utf-8"),
    ]
    if image_path is not None:
        command += ["--allowedTools", "Read"]
    else:
        command += ["--tools", ""]
    return command


def run_claude(prompt: str, image_path: Optional[Path]) -> str:
    if image_path is not None:
        prompt = f"먼저 Read 도구로 이미지 파일 {image_path} 을 읽어라.\n\n{prompt}"
    result = subprocess.run(
        claude_command(image_path), input=prompt, capture_output=True, text=True, timeout=parser_timeout(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:] or f"claude exit={result.returncode}")
    return result.stdout


def parser_timeout() -> int:
    return int(os.environ.get("BIZCARD_PARSER_TIMEOUT", os.environ.get("BIZCARD_CODEX_TIMEOUT", "90")))


def extract_json(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    candidates = [text.strip()]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None
