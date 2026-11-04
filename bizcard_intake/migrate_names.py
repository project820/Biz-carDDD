"""One-off rename of contacts saved with the old name mapping.

Old layout: givenName "최민준 차장", familyName "(주) 다솔이엔지" (= organization
name), so phones listed the company first. New layout: givenName
"최민준 차장 (주)다솔이엔지" and no familyName; organizations stay as they are.

    python -m bizcard_intake.migrate_names            # show the plan only
    python -m bizcard_intake.migrate_names --apply    # back up, then rename
    python -m bizcard_intake.migrate_names --rollback data/name-migration/<file>.json

The backup JSON is written before the first update and rewritten after every
contact with its status, so an interrupted run can still be rolled back.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .contacts import ContactSaveError, get_contact, iter_connections, run_gws
from .draft import CardDraft

DEFAULT_BACKUP_DIR = Path("data/name-migration")
WRITABLE_NAME_KEYS = (
    "givenName",
    "familyName",
    "middleName",
    "honorificPrefix",
    "honorificSuffix",
    "phoneticFullName",
    "phoneticGivenName",
    "phoneticFamilyName",
    "phoneticMiddleName",
    "phoneticHonorificPrefix",
    "phoneticHonorificSuffix",
)
REASON = "성(familyName)에 회사명이 들어 있음"


def plan_migration() -> list[dict[str, Any]]:
    plan = []
    for person in iter_connections("names,organizations,metadata"):
        item = plan_item(person)
        if item:
            plan.append(item)
    return plan


def plan_item(person: dict[str, Any]) -> Optional[dict[str, Any]]:
    names = person.get("names") or []
    if not names or not person.get("resourceName"):
        return None
    first = names[0]
    family = _squash(first.get("familyName", ""))
    if not family:
        return None
    org = next((o for o in person.get("organizations", []) if _squash(o.get("name", "")) == family), None)
    if org is None:
        return None
    tokens = (first.get("givenName") or "").split()
    if not tokens:
        return None
    draft = CardDraft(name=tokens[0], title=org.get("title") or " ".join(tokens[1:]), company=org.get("name", ""))
    new_given = draft.display_name
    if not new_given or new_given == current_display(first):
        return None
    return {
        "resourceName": person["resourceName"],
        "etag": person.get("etag", ""),
        "old_names": names,
        "new_given_name": new_given,
        "reason": REASON,
    }


def current_display(name: dict[str, Any]) -> str:
    return name.get("displayName") or " ".join(p for p in (name.get("givenName"), name.get("familyName")) if p)


def format_plan(plan: list[dict[str, Any]]) -> str:
    if not plan:
        return "이름 정리 대상 없음"
    lines = [f"이름 정리 대상 {len(plan)}건"]
    for index, item in enumerate(plan, 1):
        old = item["old_names"][0] if item["old_names"] else {}
        before = " / ".join(p for p in (old.get("givenName"), old.get("familyName")) if p)
        lines.append(f"{index}. {before} -> {item['new_given_name']}  ({item['resourceName']})")
    return "\n".join(lines)


def apply_migration(
    plan: list[dict[str, Any]],
    backup_dir: Path,
    only: Optional[set[str]] = None,
    limit: Optional[int] = None,
) -> Path:
    """Rename the planned contacts one by one; returns the backup JSON path."""
    items = [item for item in plan if only is None or item["resourceName"] in only]
    if limit is not None:
        items = items[:limit]
    records = [
        {
            "resourceName": item["resourceName"],
            "etag": item.get("etag", ""),
            "old_names": item["old_names"],
            "new_given_name": item["new_given_name"],
            "status": "pending",
        }
        for item in items
    ]
    backup = {"created_at": datetime.now().isoformat(timespec="seconds"), "items": records}
    backup_path = _new_backup_path(Path(backup_dir))
    _write(backup_path, backup)  # before any update

    for record in records:
        try:
            fresh = get_contact(record["resourceName"], "names,metadata")
            old_given = (record["old_names"][0] or {}).get("givenName", "") if record["old_names"] else ""
            fresh_given = ((fresh.get("names") or [{}])[0] or {}).get("givenName", "")
            if fresh_given != old_given:
                record.update(status="skipped", error=f"계획 이후 이름이 바뀜: {fresh_given}")
            else:
                _update_names(record["resourceName"], fresh.get("etag", ""), [{"givenName": record["new_given_name"]}])
                record["status"] = "ok"
        except ContactSaveError as exc:
            record.update(status="error", error=str(exc), kind=exc.kind)
        _write(backup_path, backup)
    return backup_path


def rollback(backup_path: Path) -> list[dict[str, Any]]:
    """Restore the names saved in a backup. Contacts whose givenName is no
    longer the migrated value (edited since, or never migrated) are skipped."""
    backup = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    results = []
    for record in backup.get("items", []):
        result = {"resourceName": record["resourceName"]}
        try:
            fresh = get_contact(record["resourceName"], "names,metadata")
            current = ((fresh.get("names") or [{}])[0] or {}).get("givenName", "")
            if current != record["new_given_name"]:
                result.update(status="skipped", error=f"현재 이름이 다름: {current}")
            else:
                names = restorable_names(record.get("old_names", []))
                if not names:
                    result.update(status="skipped", error="복원할 이름 없음")
                else:
                    _update_names(record["resourceName"], fresh.get("etag", ""), names)
                    result["status"] = "ok"
        except ContactSaveError as exc:
            result.update(status="error", error=str(exc), kind=exc.kind)
        results.append(result)
    return results


def restorable_names(old_names: list[dict[str, Any]]) -> list[dict[str, Any]]:
    restored = []
    for name in old_names:
        source = ((name.get("metadata") or {}).get("source") or {}).get("type")
        if source not in (None, "CONTACT"):
            continue  # profile names are read-only
        kept = {key: name[key] for key in WRITABLE_NAME_KEYS if name.get(key)}
        if kept:
            restored.append(kept)
    return restored


def _update_names(resource_name: str, etag: str, names: list[dict[str, Any]]) -> None:
    body: dict[str, Any] = {"resourceName": resource_name, "names": names}
    if etag:
        body["etag"] = etag
    params = {"resourceName": resource_name, "updatePersonFields": "names", "personFields": "names"}
    run_gws(
        ["people", "people", "updateContact", "--params", json.dumps(params), "--json", json.dumps(body, ensure_ascii=False)],
        mutating=True,
    )


def _new_backup_path(backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"name-migration-{stamp}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = backup_dir / f"name-migration-{stamp}-{counter}.json"
    return path


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _squash(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="옛 방식(성=회사명)으로 저장된 연락처 이름 정리")
    parser.add_argument("--apply", action="store_true", help="백업 후 실제로 이름 변경")
    parser.add_argument("--only", nargs="*", help="이 resourceName만 처리 (people/c...)")
    parser.add_argument("--limit", type=int, help="최대 처리 건수")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--rollback", type=Path, help="백업 JSON으로 이전 이름 복원")
    args = parser.parse_args(argv)

    if args.rollback:
        results = rollback(args.rollback)
        for result in results:
            print(f"[{result['status']}] {result['resourceName']} {result.get('error', '')}".rstrip())
        return 0 if all(r["status"] != "error" for r in results) else 1

    plan = plan_migration()
    print(format_plan(plan))
    if not args.apply or not plan:
        if plan and not args.apply:
            print("\n실제로 바꾸려면 --apply")
        return 0
    path = apply_migration(plan, args.backup_dir, set(args.only) if args.only else None, args.limit)
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    for item in items:
        print(f"[{item['status']}] {item['resourceName']} {item.get('error', '')}".rstrip())
    print(f"백업: {path}  (복원: --rollback {path})")
    return 0 if all(i["status"] != "error" for i in items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
