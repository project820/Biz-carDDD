"""Business-card parsing: OpenAI API (default when a key exists), codex or claude CLI.

- openai: Responses API with gpt-6-luna, reasoning effort low and a strict
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
DEFAULT_REASONING_EFFORT = "low"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
# Same lookup order as the decision skill: process env, then key files.
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
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
    "배경 명함·손가락·테이블·반사광에서 온 텍스트는 무시해라. "
    "이름·회사명의 한글 철자는 바꾸지 마라. OCR 원문과 이미지가 같은 철자면 그대로 쓰고, "
    "다르면 이미지의 로고·영문 표기(예: HANA=하나, DAON=다온)와 대조해 맞는 쪽을 골라라.\n"
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


def card_prompt(ocr: OcrResult, side: str) -> str:
    side_label = "앞면" if side == "front" else "뒷면"
    raw_ocr = ocr.text[:5000] if ocr.text else "(OCR 원문 없음)"
    return (
        f"명함 {side_label}의 보정 이미지와 Apple Vision OCR 원문을 대조해 Google Contacts 입력 필드를 채워라. "
        "이름과 휴대전화가 가장 중요하고 회사명과 이메일은 보조 필드다.\n\n"
        f"{FIELD_GUIDE}\n\n"
        f"[OCR 평균 confidence: {ocr.average_confidence:.3f}]\n[OCR 원문]\n{raw_ocr}"
    )


def parse_card(image_path: Path, ocr: OcrResult, side: str) -> CardDraft:
    prompt = card_prompt(ocr, side)
    if crosscheck_enabled():
        data = run_crosscheck(prompt, image_path, ocr.text or "")
    else:
        data = run_backend(prompt, image_path=image_path)
    if data is None:
        return CardDraft(warnings=["자동 파싱 실패: 직접 입력하거나 재시도하세요"])
    return CardDraft.from_dict(data)


# ------------------------------------------------------------------ cross-check

def crosscheck_enabled() -> bool:
    """Luna + Haiku in parallel when both API keys exist (BIZCARD_CROSSCHECK=off disables)."""
    if os.environ.get("BIZCARD_CROSSCHECK", "auto").strip().lower() in ("off", "0", "false", "no"):
        return False
    return backend_name() == "openai" and bool(anthropic_api_key())


def anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def run_crosscheck(prompt: str, image_path: Path, ocr_text: str) -> Optional[dict]:
    from concurrent.futures import ThreadPoolExecutor

    def safe(fn, name):
        try:
            return fn(prompt, image_path)
        except Exception as exc:
            log.warning("%s failed in cross-check: %s", name, exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        luna = pool.submit(safe, run_openai, "openai")
        haiku = pool.submit(safe, run_anthropic, "anthropic")
        a, b = luna.result(), haiku.result()
    if a is None and b is None:
        return run_backend(prompt, image_path=image_path)  # both APIs down: CLI fallback
    if a is None or b is None:
        return a or b
    return crosscheck_merge(a, b, ocr_text)


def run_anthropic(prompt: str, image_path: Optional[Path]) -> dict:
    key = anthropic_api_key()
    if not key:
        raise RuntimeError("Anthropic API key not found")
    content: list[dict] = []
    if image_path is not None:
        suffix = Path(image_path).suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                    "data": base64.b64encode(Path(image_path).read_bytes()).decode("ascii")}})
    content.append({"type": "text", "text": prompt})
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    body = {
        "model": os.environ.get("BIZCARD_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
        "max_tokens": 2048,
        "tools": [{"name": "business_card", "description": "명함에서 읽은 연락처 필드", "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": "business_card"},
        "messages": [{"role": "user", "content": content}],
    }
    request = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=parser_timeout()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body_text).get("error", {}).get("message") or body_text
        except (json.JSONDecodeError, AttributeError):
            message = body_text
        raise RuntimeError(f"Anthropic HTTP {exc.code}: {message[:300]}") from exc
    usage = result.get("usage") or {}
    log.info("anthropic model=%s tokens in=%s out=%s", result.get("model"), usage.get("input_tokens"), usage.get("output_tokens"))
    for block in result.get("content", []):
        if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            return block["input"]
    raise RuntimeError("Anthropic returned no card fields")


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("0082"):
        digits = digits[2:]
    if digits.startswith("82") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


def _squash(value: str) -> str:
    value = re.sub(r"\(주\)|주식회사|㈜|co\.?,?\s*ltd\.?|inc\.?", "", value or "", flags=re.I)
    return re.sub(r"[\s.,·()\[\]/&-]+", "", value).lower()


def _related(x: str, y: str) -> bool:
    a, b = _squash(x), _squash(y)
    return a == b or (bool(a) and bool(b) and (a in b or b in a))


def crosscheck_merge(luna: dict, haiku: dict, ocr_text: str) -> dict:
    """Combine two independent readings of one card.

    Phones: union of both readings, keeping a number only when both models agree,
    its digits appear in the OCR text, or it is the second line of a '~' range.
    Name/company: identical -> accepted; different -> the reading found in the
    OCR text is used and a 확인 필요 warning lists both, because the OCR itself
    can be wrong (다온/다운). Other fields: Luna first, Haiku fills gaps.
    """
    merged = dict(luna)
    warnings = list(luna.get("warnings") or [])
    ocr_digit_lines = [_digits(line) for line in (ocr_text or "").splitlines()]
    ocr_digits = "|".join(d for d in ocr_digit_lines if d)
    has_range = bool(re.search(r"[~∼～]", ocr_text or ""))
    ocr_squashed = _squash(ocr_text)

    fields = ("mobile", "work", "fax")
    # A number can sit in two fields on one card (TEL 0126~7 / FAX 0127), so
    # placement is per (field, digits): Luna's fields first, Haiku adds numbers
    # Luna did not read at all. Votes are per number across both models.
    seen: dict[str, str] = {}  # digits -> first value seen (for warnings)
    votes: dict[str, int] = {}
    placed: list[tuple[str, str, str]] = []  # (field, digits, value)
    luna_keys: set[str] = set()
    for index, source in enumerate((luna, haiku)):
        source_keys: set[str] = set()
        for field in fields:
            for value in source.get(field) or []:
                key = _digits(value)
                if len(key) < 7:
                    continue
                source_keys.add(key)
                seen.setdefault(key, value)
                if index == 1 and key in luna_keys:
                    continue
                if not any(f == field and k == key for f, k, _ in placed):
                    placed.append((field, key, value))
        for key in source_keys:
            votes[key] = votes.get(key, 0) + 1
        if index == 0:
            luna_keys = source_keys
    verified = {k for k in seen if votes.get(k, 0) >= 2 or k in ocr_digits}
    for key in list(seen):
        if key in verified or not has_range:
            continue
        if any(v[:-2] == key[:-2] and v != key for v in verified):
            verified.add(key)
    dropped = [seen[k] for k in seen if k not in verified]
    for field in fields:
        merged[field] = [value for f, key, value in placed if f == field and key in verified]
    if dropped:
        log.info("cross-check dropped unverified numbers: %s", ", ".join(dropped))
        warnings.append(f"한 모델만 읽고 OCR에 없는 번호 제외: {', '.join(dropped)} (맞으면 직접 추가)")

    for field, label in (("name", "이름"), ("company", "회사명")):
        a, b = (luna.get(field) or "").strip(), (haiku.get(field) or "").strip()
        if not a or not b:
            merged[field] = a or b
        elif _related(a, b):
            merged[field] = a if len(_squash(a)) >= len(_squash(b)) else b
        else:
            a_in, b_in = _squash(a) in ocr_squashed, _squash(b) in ocr_squashed
            merged[field] = b if (b_in and not a_in) else a
            warnings.append(f"{label} 판독이 엇갈림: {a} / {b} — 맞는 쪽을 확인하세요")

    for field in ("title", "display_title", "notes"):
        merged[field] = (luna.get(field) or "").strip() or (haiku.get(field) or "").strip()
    for field in ("email", "website", "address"):
        merged[field] = list(luna.get(field) or []) or list(haiku.get(field) or [])
    for field in ("email", "website"):
        extra = [v for v in haiku.get(field) or [] if _squash(v) not in {_squash(x) for x in merged[field]} and _squash(v) in ocr_squashed]
        merged[field] = merged[field] + extra
    merged["warnings"] = warnings
    return merged


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
