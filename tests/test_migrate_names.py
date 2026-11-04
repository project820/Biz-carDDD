import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import contacts, migrate_names
from bizcard_intake.contacts import ContactSaveError

LEGACY = {
    "resourceName": "people/c1",
    "etag": "e-plan",
    "names": [{"metadata": {"source": {"type": "CONTACT"}}, "displayName": "(주) 다솔이엔지 최민준 차장", "givenName": "최민준 차장", "familyName": "(주) 다솔이엔지", "phoneticFamilyName": "다솔"}],
    "organizations": [{"name": "(주) 다솔이엔지", "title": "차장"}],
}
NO_TITLE_ORG = {
    "resourceName": "people/c2",
    "etag": "e2",
    "names": [{"givenName": "홍길동 영업팀장/컨설턴트", "familyName": "ACME"}],
    "organizations": [{"name": "A C M E"}],
}
UNRELATED = [
    {"resourceName": "people/x1", "names": [{"givenName": "길동", "familyName": "홍"}], "organizations": [{"name": "ACME"}]},
    {"resourceName": "people/x2", "names": [{"givenName": "최민준 차장 (주)다솔이엔지"}], "organizations": [{"name": "(주) 다솔이엔지"}]},
]


class FakePeople:
    def __init__(self, people):
        self.people = {p["resourceName"]: json.loads(json.dumps(p)) for p in people}
        self.updates = []
        self.fail = set()

    def __call__(self, args, allow_failure=False, expect_json=True, mutating=False):
        params = json.loads(args[args.index("--params") + 1])
        if args[2] == "connections":
            return {"connections": list(self.people.values())}
        if args[2] == "get":
            person = self.people[params["resourceName"]]
            return {"resourceName": person["resourceName"], "etag": person["etag"], "names": person["names"]}
        if args[2] == "updateContact":
            body = json.loads(args[args.index("--json") + 1])
            if params["resourceName"] in self.fail:
                raise ContactSaveError("Google 서버 일시 오류(502). 잠시 후 다시 시도합니다.", kind="transient", retryable=True)
            self.updates.append((params, body))
            person = self.people[params["resourceName"]]
            person["names"] = body["names"]
            person["etag"] = person["etag"] + "+"
            return person
        raise AssertionError(args)


class MigrateNamesTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePeople([LEGACY, NO_TITLE_ORG, *UNRELATED])
        patcher = patch.object(contacts, "run_gws", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(migrate_names, "run_gws", self.fake)
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_plan_selects_family_equals_company(self):
        plan = migrate_names.plan_migration()
        self.assertEqual([p["resourceName"] for p in plan], ["people/c1", "people/c2"])
        self.assertEqual(plan[0]["new_given_name"], "최민준 차장 (주)다솔이엔지")
        self.assertEqual(plan[0]["etag"], "e-plan")
        self.assertEqual(plan[0]["old_names"], LEGACY["names"])
        self.assertEqual(plan[1]["new_given_name"], "홍길동 영업팀장 A C M E")
        text = migrate_names.format_plan(plan)
        self.assertIn("최민준 차장 / (주) 다솔이엔지 -> 최민준 차장 (주)다솔이엔지", text)

    def test_apply_backs_up_then_renames_and_rollback_restores(self):
        plan = migrate_names.plan_migration()
        self.fake.fail.add("people/c2")
        with tempfile.TemporaryDirectory() as tmp:
            path = migrate_names.apply_migration(plan, Path(tmp))
            backup = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([i["status"] for i in backup["items"]], ["ok", "error"])
            self.assertEqual(backup["items"][0]["old_names"], LEGACY["names"])
            params, body = self.fake.updates[0]
            self.assertEqual(params["updatePersonFields"], "names")
            self.assertEqual(body["names"], [{"givenName": "최민준 차장 (주)다솔이엔지"}])
            self.assertEqual(body["etag"], "e-plan")  # fresh GET etag
            self.fake.fail.clear()

            results = migrate_names.rollback(path)
        self.assertEqual([r["status"] for r in results], ["ok", "skipped"])
        restored = self.fake.people["people/c1"]["names"]
        self.assertEqual(restored, [{"givenName": "최민준 차장", "familyName": "(주) 다솔이엔지", "phoneticFamilyName": "다솔"}])

    def test_backup_written_before_first_update_and_filters(self):
        plan = migrate_names.plan_migration()
        seen = {}
        original = self.fake.__call__

        def spy(args, **kwargs):
            if args[2] == "updateContact" and "files" not in seen:
                seen["files"] = list(Path(self.tmp).glob("*.json"))
            return original(args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, patch.object(migrate_names, "run_gws", spy):
            self.tmp = tmp
            path = migrate_names.apply_migration(plan, Path(tmp), only={"people/c2"}, limit=5)
            self.assertEqual(seen["files"], [path])
            items = json.loads(path.read_text(encoding="utf-8"))["items"]
        self.assertEqual([i["resourceName"] for i in items], ["people/c2"])
        with tempfile.TemporaryDirectory() as tmp:
            items = json.loads(migrate_names.apply_migration(migrate_names.plan_migration(), Path(tmp), limit=1).read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(items), 1)

    def test_apply_skips_contact_renamed_since_plan(self):
        plan = migrate_names.plan_migration()
        self.fake.people["people/c1"]["names"] = [{"givenName": "수동 수정"}]
        with tempfile.TemporaryDirectory() as tmp:
            items = json.loads(migrate_names.apply_migration(plan, Path(tmp), only={"people/c1"}).read_text(encoding="utf-8"))["items"]
        self.assertEqual(items[0]["status"], "skipped")
        self.assertEqual(self.fake.updates, [])


if __name__ == "__main__":
    unittest.main()
