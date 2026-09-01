"""Contract matrix for deterministic journey reference resolution."""
import time

import pytest

from dashboard import db, refs


ACCOUNT_ID = "acct_refmatrix_anchor"
PROJECT_ID = "proj_refmatrix_alpha"
PROJECT_CROSS_ID = "proj_refmatrix_cross_only"
DEAL_ID = "deal_refmatrix_enterprise"


@pytest.fixture(scope="module", autouse=True)
def _seed_refs():
    """Seed collision-resistant rows in conftest's session DB sandbox."""
    now = int(time.time())
    conn = db.get_conn()
    try:
        conn.executemany(
            "INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)",
            [
                (ACCOUNT_ID, "RefMatrix Singular Account", now),
                ("acct_refmatrix_twin_n", "RefMatrix Twin North", now),
                ("acct_refmatrix_twin_s", "RefMatrix Twin South", now),
            ],
        )
        conn.executemany(
            "INSERT INTO projects (id, slug, name, created_at) VALUES (?,?,?,?)",
            [
                (PROJECT_ID, "refmatrix-alpha-slug", "RefMatrix Alpha Project", now),
                (PROJECT_CROSS_ID, "refmatrix-cross-only", "RefMatrix Cross Kind Only", now),
            ],
        )
        conn.executemany(
            "INSERT INTO deals (id, account_id, title, stage, created_at) "
            "VALUES (?,?,?,?,?)",
            [
                (DEAL_ID, ACCOUNT_ID, "RefMatrix Enterprise Deal", "lead", now),
                ("deal_refmatrix_case", ACCOUNT_ID, "RefMatrix Case Study", "lead", now),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("case", "kind", "ref", "expected"),
    [
        (
            "id-hit",
            "account",
            ACCOUNT_ID,
            {"ok": True, "id": ACCOUNT_ID, "kind": "account", "name": "RefMatrix Singular Account"},
        ),
        (
            "slug-hit",
            "project",
            "refmatrix-alpha-slug",
            {"ok": True, "id": PROJECT_ID, "kind": "project", "name": "RefMatrix Alpha Project"},
        ),
        (
            "exact-name",
            "project",
            "RefMatrix Alpha Project",
            {"ok": True, "id": PROJECT_ID, "kind": "project", "name": "RefMatrix Alpha Project"},
        ),
        (
            "unique-prefix",
            "account",
            "RefMatrix Sing",
            {"ok": True, "id": ACCOUNT_ID, "kind": "account", "name": "RefMatrix Singular Account"},
        ),
        (
            "unique-substring",
            "deal",
            "Enterprise",
            {"ok": True, "id": DEAL_ID, "kind": "deal", "name": "RefMatrix Enterprise Deal"},
        ),
        ("not-found", "account", "refmatrix-no-such-entity", {"ok": False, "code": "not_found"}),
        (
            "case-insensitive",
            "project",
            "rEfMaTrIx AlPhA pRoJeCt",
            {"ok": True, "id": PROJECT_ID, "kind": "project", "name": "RefMatrix Alpha Project"},
        ),
        (
            "deal-by-title",
            "deal",
            "RefMatrix Case Study",
            {"ok": True, "id": "deal_refmatrix_case", "kind": "deal", "name": "RefMatrix Case Study"},
        ),
        (
            "cross-kind-isolation",
            "deal",
            "RefMatrix Cross Kind Only",
            {"ok": False, "code": "not_found"},
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and value in {
        "id-hit", "slug-hit", "exact-name", "unique-prefix", "unique-substring",
        "not-found", "case-insensitive", "deal-by-title", "cross-kind-isolation",
    } else None,
)
def test_resolve_matrix(case, kind, ref, expected):
    assert refs.resolve(kind, ref) == expected, case


def test_two_candidates_are_ambiguous_and_never_guessed():
    assert refs.resolve("account", "RefMatrix Twin") == {
        "ok": False,
        "code": "ambiguous",
        "candidates": [
            {"id": "acct_refmatrix_twin_n", "name": "RefMatrix Twin North"},
            {"id": "acct_refmatrix_twin_s", "name": "RefMatrix Twin South"},
        ],
    }
