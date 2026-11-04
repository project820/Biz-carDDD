"""Command line entry: bot (default), scan, save, retry-pending, doctor, setup."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import cards, doctor, migrate_names, pipeline, telegram
from .contacts import LOGIN_COMMAND

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANUAL_GWS_STEPS = """\
gcloud가 없어 수동 설정이 필요합니다 (한 번만):
  1. https://console.cloud.google.com 에서 새 프로젝트 생성
  2. API 및 서비스 > 라이브러리 > "People API" 사용 설정
  3. Google 인증 플랫폼 > 대상: 사용자 유형 선택
     - Workspace 계정: "내부" (만료 없음)
     - 개인 Gmail: "외부" 로 만든 뒤 반드시 "프로덕션"으로 게시 (테스트 상태는 7일마다 로그인 만료)
  4. 클라이언트 > 클라이언트 만들기 > 데스크톱 앱 > JSON 다운로드
  5. 내려받은 파일을 ~/.config/gws/client_secret.json 으로 저장
  6. 아래 로그인 명령 실행"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bizcard", description="Business card -> Google Contacts")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("bot", help="Telegram 봇 실행 (기본)")
    scan = sub.add_parser("scan", help="이미지 하나를 파싱해 초안 출력 (로컬 점검용)")
    scan.add_argument("image", type=Path)
    save = sub.add_parser("save", help="명함 ID(또는 예전 세션 JSON)를 Google Contacts에 저장")
    save.add_argument("session", type=Path)
    sub.add_parser("retry-pending", help="저장 보류된 명함 즉시 재시도")
    sub.add_parser("list", help="저장 안 된 명함 목록")
    migrate = sub.add_parser("migrate-names", help="기존 연락처 이름을 '이름 직함 회사' 형식으로 정리 (기본: 미리보기)")
    migrate.add_argument("--apply", action="store_true", help="실제로 변경 (백업 후)")
    migrate.add_argument("--only", action="append", default=[], help="people/c... 지정 (여러 번 가능)")
    migrate.add_argument("--limit", type=int, default=None)
    rollback = sub.add_parser("rollback-names", help="migrate-names 백업으로 이름 되돌리기")
    rollback.add_argument("backup", type=Path)
    sub.add_parser("doctor", help="준비 상태 점검")
    sub.add_parser("setup", help="준비 상태 점검 후 부족한 항목 안내/설정")
    args = parser.parse_args(argv)

    command = args.command or "bot"
    if command != "bot":
        telegram.setup_logging()
    if command == "bot":
        telegram.main()
        return 0
    if command == "scan":
        return cmd_scan(args.image)
    if command == "save":
        return cmd_save(args.session)
    if command == "retry-pending":
        return cmd_retry_pending()
    if command == "list":
        return cmd_list()
    if command == "migrate-names":
        return cmd_migrate_names(args.apply, set(args.only) or None, args.limit)
    if command == "rollback-names":
        return cmd_rollback_names(args.backup)
    if command == "doctor":
        return cmd_doctor()
    if command == "setup":
        return cmd_setup()
    parser.print_help()
    return 2


def cmd_scan(image: Path) -> int:
    if not image.exists():
        print(f"파일 없음: {image}", file=sys.stderr)
        return 1
    card = cards.new_card("cli", source="cli")
    target = pipeline.DATA_DIR / "cli" / "front" / f"{card['id']}_{image.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, target)
    pipeline.recognize_front(card, target)
    print(pipeline.draft_text(card))
    print(f"\n[rectifier={card.get('rectifier')}] 명함 ID: {card['id']}")
    print(f"저장하려면: python -m bizcard_intake save {card['id']}")
    return 0


def cmd_save(target: Path) -> int:
    card = cards.load_card(str(target))
    if card is None and target.exists():
        legacy = json.loads(target.read_text(encoding="utf-8"))
        card = cards.load_card(legacy.get("id", "")) if legacy.get("id") else None
        if card is None:
            card = pipeline._card_from_legacy("cli", legacy, "review")
            cards.save_card(card)
    if card is None:
        print(f"명함 ID 또는 세션 파일을 찾을 수 없음: {target}", file=sys.stderr)
        return 1
    outcome = pipeline.approve(card)
    print(("저장 완료: " if outcome["ok"] else "저장 실패: ") + outcome["message"])
    for warning in outcome.get("warnings") or []:
        print(f"  참고: {warning}")
    return 0 if outcome["ok"] else 1


def cmd_retry_pending() -> int:
    pipeline.migrate_legacy()
    saved, failures = pipeline.retry_due(force=True)
    print(f"saved {saved}, failed {len(failures)}")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


def cmd_list() -> int:
    pipeline.migrate_legacy()
    open_cards = cards.list_cards(statuses=cards.OPEN_STATUSES)
    if not open_cards:
        print("저장 안 된 명함 없음")
    for card in open_cards:
        draft = pipeline.current_draft(card)
        print(f"{card['id']}  {cards.label(card)}  {card['status']:<11} {draft.display_name}  {card.get('last_error') or ''}")
    return 0


def cmd_migrate_names(apply: bool, only, limit) -> int:
    plan = migrate_names.plan_migration()
    if only:
        plan = [item for item in plan if item["resourceName"] in only]
    if limit:
        plan = plan[:limit]
    print(migrate_names.format_plan(plan))
    if not apply:
        print(f"\n미리보기 {len(plan)}건. 적용하려면 --apply (먼저 --limit 1 권장)")
        return 0
    backup = migrate_names.apply_migration(plan, Path("data/name-migration"))
    print(f"적용 완료. 되돌리기: python -m bizcard_intake rollback-names {backup}")
    return 0


def cmd_rollback_names(backup: Path) -> int:
    results = migrate_names.rollback(backup)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor() -> int:
    checks = doctor.run_checks()
    print(doctor.format_report(checks))
    return 0 if all(c.ok for c in checks) else 1


def cmd_setup() -> int:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(PROJECT_ROOT / ".env.example", env_file)
        print(f".env 생성: {env_file}  ->  TELEGRAM_BOT_TOKEN 등을 채운 뒤 다시 실행하세요.")

    checks = doctor.run_checks()
    print(doctor.format_report(checks))
    failed = {c.name: c for c in checks if not c.ok}
    if not failed:
        print("\n모든 준비가 끝났습니다. 실행: ./scripts/start_bot.sh")
        return 0

    gws = failed.get("gws")
    if gws and shutil.which("gws"):
        print("\n--- Google 로그인 ---")
        if shutil.which("gcloud"):
            if ask("gcloud가 있습니다. gws auth setup --login 으로 프로젝트/OAuth 클라이언트를 자동 생성할까요?"):
                subprocess.run(["gws", "auth", "setup", "--login"], check=False)
        elif not (Path.home() / ".config" / "gws" / "client_secret.json").exists():
            print(MANUAL_GWS_STEPS)
        if ask(f"지금 로그인할까요? ({LOGIN_COMMAND})"):
            subprocess.run(LOGIN_COMMAND.split(), check=False)
        print("개인 Gmail(외부 앱)이라면 콘솔에서 앱을 '프로덕션'으로 게시했는지 확인하세요. 테스트 상태는 7일마다 로그인이 풀립니다.")

    parser_check = failed.get("parser")
    if parser_check:
        print("\n--- 명함 파싱 모델 ---")
        print("ChatGPT(Plus 이상) 구독이면 codex, Claude 구독이면 claude CLI를 설치하고 로그인하세요. .env의 BIZCARD_PARSER로 선택합니다.")
        print(f"  {parser_check.fix}")

    print("\n남은 항목을 해결한 뒤 `python -m bizcard_intake doctor` 로 다시 확인하세요.")
    return 1


def ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
