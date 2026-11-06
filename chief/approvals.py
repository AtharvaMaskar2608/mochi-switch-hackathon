import secrets

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


class Approvals:
    def __init__(self):
        self._pending: dict[str, tuple[str, str, dict]] = {}

    def request(self, action: str, owner_slug: str, payload: dict) -> str:
        while (code := "".join(secrets.choice(ALPHABET) for _ in range(4))) in self._pending:
            pass
        self._pending[code] = (action, owner_slug, payload)
        return code

    def resolve(self, code: str, by_slug: str, yes: bool) -> dict | None:
        """Payload if by_slug owns it (code is then spent); else None. The caller acts on `yes`."""
        entry = self._pending.get(code.upper())
        if not entry or entry[1] != by_slug:
            return None
        del self._pending[code.upper()]
        return entry[2]
