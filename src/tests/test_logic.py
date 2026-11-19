import json
import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC_ROOT))
sys.path.insert(0, str(SRC_ROOT / "scripts"))

import catalog_api  # noqa: E402
import events_audit  # noqa: E402
import verify_e2e  # noqa: E402


class Context:
    def getRequestID(self):
        return "unit-request"


class MultiTenantLogicTests(unittest.TestCase):
    def test_local_end_to_end_contract(self):
        result = verify_e2e.local_verify()
        self.assertTrue(result["passed"])
        self.assertTrue(all(result["checks"].values()))

    def test_tenant_key_prefix(self):
        self.assertEqual(catalog_api._tenant_key("abc"), "TENANT#abc")

    def test_audit_response_contract(self):
        response = events_audit.handler(
            {
                "id": "event-1",
                "source": "mtsaas-tenant-lifecycle",
                "type": "tenant.status_changed",
                "data": {"tenant_id": "tenant-1", "status": "Inactive"},
            },
            Context(),
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertFalse(response["isBase64Encoded"])
        self.assertEqual(json.loads(response["body"])["event_id"], "event-1")


if __name__ == "__main__":
    unittest.main()
