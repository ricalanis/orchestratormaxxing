"""Product catalog (Strategy → productized offers) — regression guard.

Covers:
  1. dashboard/db.py     — products table + raw CRUD helpers
  2. dashboard/growth.py — seed defaults, validation, list/create/update/delete
  3. GET/POST/PATCH/DELETE /api/growth/products endpoints

Isolation (same good-citizen pattern as test_icp_config): point the DB layer at a
COPY of kanban.db, ensure schema on the copy, build the client, then RESTORE the
shared _db.KANBAN_DB. Each test re-points in setUp and wipes the products table.

Run:  python -m pytest tests/test_products.py -v
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
_TMP_DB = None
_db = None
_growth = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_prod_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)

        _ORIG_KDB = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        try:
            from dashboard import growth as _growth
            _growth.ensure_schema()
            from dashboard.api import app
            from starlette.testclient import TestClient
            _CLIENT = TestClient(app, raise_server_exceptions=False)
            _READY = True
        finally:
            _db.KANBAN_DB = _ORIG_KDB          # good citizen: don't leak our copy
except Exception:  # pragma: no cover
    _READY = False


@atexit.register
def _cleanup_tmp_db():  # pragma: no cover
    try:
        if _TMP_DB and _TMP_DB.exists():
            _TMP_DB.unlink()
    except Exception:
        pass


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class ProductBase(unittest.TestCase):
    def setUp(self):
        self._saved = _db.KANBAN_DB
        _db.KANBAN_DB = _TMP_DB
        _db.ensure_products_schema()
        conn = _db.get_conn()
        try:
            conn.execute("DELETE FROM products")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        _db.KANBAN_DB = self._saved


class Seeding(ProductBase):
    def test_list_seeds_nine_defaults_when_empty(self):
        out = _growth.list_products()
        self.assertEqual(len(out["products"]), 9)
        names = {p["name"] for p in out["products"]}
        self.assertEqual(names, {
            # Track A — Datos → IA
            "Data & AI Readiness Scorecard", "Data & AI Readiness Audit",
            "Data Roadmap Sprint", "AI-Ready Data Stack", "Fractional CDO",
            # Track B — Producto con Agentes
            "AI Agent Readiness Scorecard", "Agent Prototype Sprint",
            "AI-Native MVP Build", "Fractional Head of Product & Engineering"})
        # each default is pinned to a valid ladder rung
        for p in out["products"]:
            self.assertIn(p["value_ladder_stage"], _growth.VALUE_LADDER)

    def test_seed_is_idempotent(self):
        _growth.list_products()               # seeds
        _growth.list_products()               # must NOT re-seed
        self.assertEqual(len(_db.products_all()), 9)

    def test_seed_skipped_when_non_empty(self):
        _growth.create_product(name="Custom offer")
        out = _growth.list_products()          # already has 1 → no seeding
        self.assertEqual(len(out["products"]), 1)


class Crud(ProductBase):
    def test_create_returns_row_with_id(self):
        res = _growth.create_product(
            name="Workshop", description="1-day", value_ladder_stage="entrada",
            fixed_price_mxn=12000)
        self.assertEqual(res["status"], "ok")
        p = res["product"]
        self.assertTrue(p["id"].startswith("prod_"))
        self.assertEqual(p["name"], "Workshop")
        self.assertEqual(p["value_ladder_stage"], "entrada")
        self.assertEqual(p["fixed_price_mxn"], 12000.0)

    def test_create_requires_name(self):
        self.assertEqual(_growth.create_product(name="  ")["status"], "error")

    def test_create_rejects_bad_stage(self):
        r = _growth.create_product(name="X", value_ladder_stage="platinum")
        self.assertEqual(r["status"], "error")

    def test_create_rejects_bad_price(self):
        self.assertEqual(_growth.create_product(name="X", fixed_price_mxn="free")["status"], "error")
        self.assertEqual(_growth.create_product(name="X", fixed_price_mxn=-1)["status"], "error")

    def test_create_allows_empty_price_and_stage(self):
        r = _growth.create_product(name="TBD offer")
        self.assertEqual(r["status"], "ok")
        self.assertIsNone(r["product"]["fixed_price_mxn"])
        self.assertIsNone(r["product"]["value_ladder_stage"])

    def test_update_patches_fields(self):
        pid = _growth.create_product(name="Old", fixed_price_mxn=1000)["product"]["id"]
        res = _growth.update_product(pid, {
            "name": "New name", "value_ladder_stage": "core", "fixed_price_mxn": 90000})
        self.assertEqual(res["status"], "ok")
        p = res["product"]
        self.assertEqual(p["name"], "New name")
        self.assertEqual(p["value_ladder_stage"], "core")
        self.assertEqual(p["fixed_price_mxn"], 90000.0)

    def test_update_missing_is_error(self):
        self.assertEqual(_growth.update_product("prod_nope", {"name": "x"})["status"], "error")

    def test_update_rejects_empty_name(self):
        pid = _growth.create_product(name="Keep")["product"]["id"]
        self.assertEqual(_growth.update_product(pid, {"name": "  "})["status"], "error")

    def test_update_ignores_unknown_keys(self):
        pid = _growth.create_product(name="Safe")["product"]["id"]
        # a non-whitelisted key (would be an injection risk) is silently ignored
        res = _growth.update_product(pid, {"id": "hacked", "created_at": 0})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["product"]["id"], pid)

    def test_delete(self):
        pid = _growth.create_product(name="Doomed")["product"]["id"]
        self.assertEqual(_growth.delete_product(pid)["status"], "ok")
        self.assertIsNone(_db.product_get(pid))
        self.assertEqual(_growth.delete_product(pid)["status"], "error")  # already gone


class Endpoints(ProductBase):
    def test_get_seeds_and_returns_list(self):
        r = _CLIENT.get("/api/growth/products")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["products"]), 9)

    def test_post_creates(self):
        r = _CLIENT.post("/api/growth/products", json={
            "name": "Retainer XL", "value_ladder_stage": "recurrente",
            "fixed_price_mxn": 60000, "description": "big"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["name"], "Retainer XL")

    def test_post_invalid_is_400(self):
        r = _CLIENT.post("/api/growth/products", json={"name": ""})
        self.assertEqual(r.status_code, 400)

    def test_patch_updates(self):
        pid = _CLIENT.post("/api/growth/products", json={"name": "P"}).json()["product"]["id"]
        r = _CLIENT.patch(f"/api/growth/products/{pid}", json={"fixed_price_mxn": 5000})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["fixed_price_mxn"], 5000.0)

    def test_patch_missing_is_404(self):
        r = _CLIENT.patch("/api/growth/products/prod_missing", json={"name": "x"})
        self.assertEqual(r.status_code, 404)

    def test_delete_endpoint(self):
        pid = _CLIENT.post("/api/growth/products", json={"name": "Bye"}).json()["product"]["id"]
        self.assertEqual(_CLIENT.delete(f"/api/growth/products/{pid}").status_code, 200)
        self.assertEqual(_CLIENT.delete(f"/api/growth/products/{pid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
