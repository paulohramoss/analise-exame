"""
Supabase em memória para os testes de ponta a ponta.

Implementa só o pedaço do cliente postgrest que core/db.py usa (select, insert,
update, upsert, delete, eq/neq/in_/is_/lte/not_, order, limit, range, rpc), para
o app rodar de verdade — rotas, templates, cifra do prontuário, consultas — sem
banco nem rede. Embeds do tipo "clientes(email, nome)" são resolvidos pela
coluna <tabela>_id… ou cliente_id.
"""

import re
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

_EMBED = re.compile(r"(\w+)\(([^)]*)\)")


class _Not:
    def __init__(self, query):
        self._query = query

    def is_(self, column, value):
        return self._query._filter(column, "is", value, negate=True)

    def eq(self, column, value):
        return self._query._filter(column, "eq", value, negate=True)


class _Query:
    def __init__(self, store, table):
        self._store, self._table = store, table
        self._op, self._payload, self._filters = "select", None, []
        self._order, self._limit, self._range, self._columns = [], None, None, "*"
        self._on_conflict = None

    # ── operação ──
    def select(self, columns="*", **_kwargs):
        self._columns = columns
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def upsert(self, payload, on_conflict=None, **_kwargs):
        self._op, self._payload, self._on_conflict = "upsert", payload, on_conflict
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    # ── filtros ──
    def _filter(self, column, op, value, negate=False):
        self._filters.append((column, op, value, negate))
        return self

    def eq(self, column, value):
        return self._filter(column, "eq", value)

    def neq(self, column, value):
        return self._filter(column, "neq", value)

    def in_(self, column, values):
        return self._filter(column, "in", list(values))

    def is_(self, column, value):
        return self._filter(column, "is", value)

    def lte(self, column, value):
        return self._filter(column, "lte", value)

    def gte(self, column, value):
        return self._filter(column, "gte", value)

    @property
    def not_(self):
        return _Not(self)

    def order(self, column, desc=False, **_kwargs):
        self._order.append((column, desc))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    # ── execução ──
    def _match(self, row):
        for column, op, value, negate in self._filters:
            current = row.get(column)
            if op == "eq":
                ok = str(current) == str(value) if current is not None else False
            elif op == "neq":
                ok = str(current) != str(value)
            elif op == "in":
                ok = current in value or str(current) in {str(v) for v in value}
            elif op == "is":
                ok = current is None if value in (None, "null") else current == value
            elif op == "lte":
                ok = current is not None and str(current) <= str(value)
            elif op == "gte":
                ok = current is not None and str(current) >= str(value)
            else:
                raise NotImplementedError(op)
            if ok == negate:
                return False
        return True

    def _project(self, row):
        out = dict(row)
        for name, _cols in _EMBED.findall(self._columns or ""):
            fk = row.get(f"{name.rstrip('s')}_id") or row.get("cliente_id")
            ref = next((r for r in self._store.tables.get(name, []) if r.get("id") == fk), None)
            out[name] = dict(ref) if ref else None
        return out

    def execute(self):
        with self._store.lock:
            rows = self._store.tables.setdefault(self._table, [])
            if self._op in {"insert", "upsert"}:
                payloads = self._payload if isinstance(self._payload, list) else [self._payload]
                saved = []
                for payload in payloads:
                    existing = None
                    if self._op == "upsert" and self._on_conflict:
                        existing = next((r for r in rows if r.get(self._on_conflict) == payload.get(self._on_conflict)), None)
                    if existing is not None:
                        existing.update(payload)
                        saved.append(dict(existing))
                        continue
                    now = datetime.now(timezone.utc).isoformat()
                    row = {"id": str(uuid.uuid4()), "created_at": now, **self._store.defaults.get(self._table, {}), **payload}
                    rows.append(row)
                    saved.append(dict(row))
                return SimpleNamespace(data=saved)

            matched = [r for r in rows if self._match(r)]
            if self._op == "update":
                for r in matched:
                    r.update(self._payload)
                return SimpleNamespace(data=[dict(r) for r in matched])
            if self._op == "delete":
                self._store.tables[self._table] = [r for r in rows if r not in matched]
                return SimpleNamespace(data=[dict(r) for r in matched])

            result = [self._project(r) for r in matched]
            for column, desc in reversed(self._order):
                result.sort(key=lambda r: (r.get(column) is None, str(r.get(column) or "")), reverse=desc)
            if self._range:
                result = result[self._range[0]:self._range[1] + 1]
            if self._limit is not None:
                result = result[: self._limit]
            return SimpleNamespace(data=result)


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.lock = threading.RLock()
        self.defaults = {
            "clientes": {"sessao_versao": 0},
            "lembretes": {"concluido_em": None, "email_enviado_em": None},
            "metas_paciente": {"concluida_em": None},
        }

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        if name == "somar_gasto_diario":
            with self.lock:
                rows = self.tables.setdefault("gastos_diarios", [])
                row = next((r for r in rows if r["escopo"] == params["p_escopo"] and r["dia"] == params["p_dia"]), None)
                if row is None:
                    row = {"escopo": params["p_escopo"], "dia": params["p_dia"], "valor": 0.0}
                    rows.append(row)
                row["valor"] += params["p_valor"]
                return SimpleNamespace(execute=lambda: SimpleNamespace(data=row["valor"]))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))
