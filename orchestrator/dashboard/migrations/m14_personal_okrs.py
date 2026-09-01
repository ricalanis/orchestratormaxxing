"""m14 — the personal OKR catalog plus immutable aggregate check-ins."""


def m14_personal_okrs(conn) -> None:
    from ..okrs import install_schema
    install_schema(conn)
