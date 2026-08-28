"""Enterprise hardening tests: PII, encryption, RBAC, audit, governance,
REST server, concurrency. These are the controls a buyer's security
review blocks on."""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cortexm.api.memory import Memory
from cortexm.config import Config
from cortexm.security.pii import PIIGuard, PIIVault, scan, redact_inplace
from cortexm.security.crypto import AESGCMCipher
from cortexm.security.rbac import APIKeyStore, authorize, RBACError


# ---------------------------------------------------------------- PII
class TestPII:
    def test_detects_all_types(self):
        text = ("email john.doe@acme.com phone +1 (415) 555-0132 "
                "card 4111 1111 1111 1111 ssn 123-45-6789 "
                "key sk-abcdefghijklmnopqrstuvwx ip 10.0.42.7")
        kinds = {s.kind for s in scan(text)}
        assert kinds == {"EMAIL", "PHONE", "CREDIT_CARD", "SSN",
                         "API_KEY", "IP"}

    def test_luhn_rejects_invalid_cards(self):
        assert scan("card 4111 1111 1111 1112") == []

    def test_ssn_rules(self):
        assert scan("ssn 000-45-6789") == []     # area 000 invalid
        assert scan("ssn 666-45-6789") == []     # area 666 invalid
        assert len(scan("ssn 123-45-6789")) == 1

    def test_redact_inplace(self):
        out = redact_inplace("mail me at a@b.co please")
        assert "a@b.co" not in out
        assert "«PII:EMAIL»" in out

    def test_memory_redact_mode(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db"), pii_mode="redact"))
            m.add([{"role": "user",
                    "content": "My email is alice@corp.com and I work at Google."}],
                  user_id="alice")
            facts = m.get_all(user_id="alice")["results"]
            blob = json.dumps(facts)
            assert "alice@corp.com" not in blob
            assert "Google" in blob  # non-PII facts flow normally
            # the redaction token lives in the stored chunk (source text)
            import re
            chunk_row = m.store.conn.execute(
                "SELECT text FROM chunks LIMIT 1").fetchone()
            tok = re.search(r"«PII:EMAIL:[0-9a-f]+»", chunk_row["text"])
            assert tok, "token must replace the raw email in the chunk"
            # reversible: DSAR resolution
            assert m.pii_vault.resolve(tok.group(0)) == "alice@corp.com"
            m.close()

    def test_memory_block_mode(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db"), pii_mode="block"))
            out = m.add("card 4111 1111 1111 1111 for billing",
                        user_id="bob")
            assert out.get("blocked") == "pii_policy"
            assert m.get_all(user_id="bob")["results"] == []
            m.close()

    def test_benchmark_mode_unaffected(self):
        # pii off by default — μ=0 benchmarks unaffected
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            m.add("email me at z@z.io", user_id="u")
            row = m.store.conn.execute(
                "SELECT text FROM chunks LIMIT 1").fetchone()
            assert "z@z.io" in row["text"]
            m.close()


# ---------------------------------------------------------------- crypto
class TestCrypto:
    def _store(self):
        class S:
            def __init__(self):
                self.kv = {}

            def kv_get(self, k, d=None):
                return self.kv.get(k, d)

            def kv_set(self, k, v):
                self.kv[k] = v
        return S()

    def test_roundtrip(self):
        import secrets
        c = AESGCMCipher(secrets.token_bytes(32))
        assert c.decrypt_str(c.encrypt_str("secret")) == "secret"

    def test_dek_persistence(self):
        import secrets
        key = secrets.token_bytes(32)
        store = self._store()
        c1 = AESGCMCipher(key, store=store)
        enc = c1.encrypt_str("persisted")
        c2 = AESGCMCipher(key, store=store)   # same key + wrapped DEK
        assert c2.decrypt_str(enc) == "persisted"

    def test_wrong_master_key_fails(self):
        import secrets
        store = self._store()
        AESGCMCipher(secrets.token_bytes(32), store=store)
        with pytest.raises(Exception):
            AESGCMCipher(secrets.token_bytes(32), store=store)

    def test_ciphertext_is_opaque(self):
        import secrets
        c = AESGCMCipher(secrets.token_bytes(32))
        enc = c.encrypt_str("top secret value")
        assert "top secret" not in enc

    def test_rotation_keeps_data_key(self):
        import secrets
        store = self._store()
        c = AESGCMCipher(secrets.token_bytes(32), store=store)
        enc = c.encrypt_str("rotatable")
        dek = c.rotate(secrets.token_bytes(32))
        c2 = AESGCMCipher(secrets.token_bytes(32), dek=dek)
        assert c2.decrypt_str(enc) == "rotatable"


# ---------------------------------------------------------------- RBAC
class TestRBAC:
    def test_key_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            meta = m.keys.create("operator", label="ci-bot")
            assert meta["key"].startswith("ctxm_operator_")
            v = m.keys.verify(meta["key"])
            assert v and v["role"] == "operator"
            assert m.keys.revoke(v["id"])
            assert m.keys.verify(meta["key"]) is None
            m.close()

    def test_key_digest_not_plaintext(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            meta = m.keys.create("admin")
            # the raw key must never be stored
            raw = json.dumps([v for _, v in m.store.iter_kv("rbac:")])
            assert meta["key"] not in raw
            m.close()

    def test_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            reader = m.keys.create("reader")["key"]
            meta = m.keys.verify(reader)
            authorize(meta, "memory.search")           # allowed
            with pytest.raises(RBACError):
                authorize(meta, "memory.add")          # denied
            with pytest.raises(RBACError):
                authorize(meta, "governance.erase")    # denied
            m.close()

    def test_expiry(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            meta = m.keys.create("reader", ttl_seconds=-1)  # already expired
            assert m.keys.verify(meta["key"]) is None
            m.close()


# ---------------------------------------------------------------- audit
class TestAudit:
    def test_chain_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            for i in range(5):
                m.audit_log.log("memory.add", actor=f"u{i}",
                            resource=f"res{i}")
            v = m.audit_log.verify()
            assert v["intact"] and v["records"] == 5
            # tamper with row 3
            m.store.conn.execute(
                "UPDATE audit_log SET actor='mallory' WHERE seq=3")
            m.store.conn.commit()
            v2 = m.audit_log.verify()
            assert not v2["intact"] and v2["first_broken_seq"] == 3
            m.close()

    def test_exports(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            m.audit_log.log("keys.create", actor="admin", resource="key_1")
            p1, p2 = os.path.join(d, "a.jsonl"), os.path.join(d, "a.log")
            assert m.audit_log.export_jsonl(p1) >= 1
            assert m.audit_log.export_syslog(p2) >= 1
            line = open(p1).readline()
            assert json.loads(line)["action"] == "keys.create"
            assert "<134>" in open(p2).readline()
            m.close()


# ---------------------------------------------------------------- governance
class TestGovernance:
    def test_erase_user_removes_everything(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            m.add([{"role": "user", "content": "I work at Google. "
                    "My email is g@corp.io."}], user_id="alice",
                   timestamp="2026-01-01T10:00:00")
            m.add("I live in Paris.", user_id="bob")
            out = m.governance.erase_user("alice")
            assert out["erased"]
            assert out["residual"]["facts"] == 0
            assert m.get_all(user_id="alice")["results"] == []
            assert len(m.get_all(user_id="bob")["results"]) > 0
            # audit chain survives with attestation
            events = m.audit_log.tail(10, action="governance.erase")
            assert events and events[0]["resource"] == "alice"
            assert m.audit_log.verify()["intact"]
            m.close()

    def test_retention(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            m.add("I work at Google.", user_id="u",
                  timestamp="2020-01-01T00:00:00")
            dry = m.governance.apply_retention(365, dry_run=True)
            assert dry["stale_facts"] > 0 and not dry["applied"]
            wet = m.governance.apply_retention(365)
            assert wet["applied"]
            m.close()

    def test_snapshot_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "m.db")
            m = Memory(Config(db_path=db))
            m.add("I work at Google.", user_id="u")
            snap = m.governance.snapshot(os.path.join(d, "snap.db"))
            assert os.path.exists(snap["path"])
            # mutate after snapshot
            m.add("I live in Tokyo.", user_id="u")
            assert len(m.get_all(user_id="u")["results"]) >= 2
            out = m.governance.restore(snap["path"])
            assert out["restored"] == db
            facts = m.get_all(user_id="u")["results"]
            assert all("Tokyo" not in f["memory"] for f in facts)
            assert any("Google" in f["memory"] for f in facts)
            m.close()

    def test_snapshot_detects_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "m.db")
            m = Memory(Config(db_path=db))
            m.add("hello", user_id="u")
            snap = m.governance.snapshot(os.path.join(d, "snap.db"))
            # corrupt the snapshot bytes
            with open(snap["path"], "r+b") as fh:
                fh.seek(512)
                fh.write(b"\x00\x01\x02\x03")
            with pytest.raises(ValueError):
                m.governance.restore(snap["path"])
            m.close()

    def test_pitr_state_at(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "t.db")))
            m.add("I work at Google.", user_id="u",
                  timestamp="2026-01-01T10:00:00")
            m.add("I work at Stripe.", user_id="u",
                  timestamp="2026-06-01T10:00:00")
            # transaction-time replay: in March the system knew Google but
            # not Stripe (Stripe's tx_from is June); by July it knew both
            early = m.governance.state_at("2026-03-01T00:00:00", user_id="u")
            vals = [f["value"] for f in early]
            assert "Google" in vals and "Stripe" not in vals
            late = m.governance.state_at("2026-07-01T00:00:00", user_id="u")
            lvals = [f["value"] for f in late]
            assert "Stripe" in lvals
            # superseded Google is gone from the late view (tx_to closed it)
            assert "Google" not in lvals or any(
                f["value"] == "Google" and f["tx_to"] for f in late)
            m.close()


# ---------------------------------------------------------------- REST
class TestRESTServer:
    @pytest.fixture()
    def server(self):
        import socket
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(db_path=os.path.join(d, "srv.db"))
            m = Memory(cfg)
            admin = m.keys.create("admin", label="test")["key"]
            reader = m.keys.create("reader", label="ro")["key"]
            from cortexm.server.rest import serve
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            httpd = serve(m, "127.0.0.1", port)
            th = threading.Thread(target=httpd.serve_forever, daemon=True)
            th.start()
            yield {"port": port, "admin": admin, "reader": reader,
                   "memory": m, "dir": d}
            httpd.shutdown()
            m.close()

    def _req(self, port, method, path, key=None, body=None):
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_health_openapi_metrics(self, server):
        p = server["port"]
        s, _ = self._req(p, "GET", "/healthz")
        assert s == 200
        s, spec = self._req(p, "GET", "/openapi.json")
        assert s == 200 and spec["openapi"].startswith("3.")
        url = f"http://127.0.0.1:{p}/metrics"
        with urllib.request.urlopen(url, timeout=5) as r:
            body = r.read().decode()
        assert "contextm_llm_calls_total" in body

    def test_auth_required(self, server):
        s, out = self._req(server["port"], "POST", "/v1/search",
                           body={"query": "x"})
        assert s == 401

    def test_rbac_denies_reader_write(self, server):
        s, out = self._req(server["port"], "POST", "/v1/add",
                           server["reader"],
                           body={"messages": "hi", "user_id": "u"})
        assert s == 403

    def test_add_search_flow(self, server):
        p, key = server["port"], server["admin"]
        s, out = self._req(p, "POST", "/v1/add", key,
                           body={"messages": "I work at Google.",
                                 "user_id": "alice"})
        assert s == 200 and out["results"]
        s, out = self._req(p, "POST", "/v1/search", key,
                           body={"query": "Where does Alice work?",
                                 "user_id": "alice"})
        assert s == 200
        assert any("Google" in r["memory"] for r in out["results"])
        s, out = self._req(p, "GET", "/v1/stats", key)
        assert s == 200 and out["facts"] >= 1

    def test_invalid_key(self, server):
        s, _ = self._req(server["port"], "GET", "/v1/stats",
                         "ctxm_admin_deadbeef")
        assert s == 401

    def test_erase_endpoint(self, server):
        p, key = server["port"], server["admin"]
        self._req(p, "POST", "/v1/add", key,
                  body={"messages": "I work at Google.", "user_id": "gdpr"})
        s, out = self._req(p, "POST", "/v1/erase", key,
                           body={"user_id": "gdpr"})
        assert s == 200 and out["erased"]
        s, out = self._req(p, "POST", "/v1/search", key,
                           body={"query": "work", "user_id": "gdpr"})
        assert s == 200 and not out["results"]


# ---------------------------------------------------------------- concurrency
class TestConcurrency:
    def test_parallel_writers(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "c.db")))
            errors = []

            def worker(i):
                try:
                    for j in range(5):
                        m.add(f"User {i} message {j}: I like item {i}-{j}.",
                              user_id=f"user{i}")
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors
            total = sum(len(m.get_all(user_id=f"user{i}")["results"])
                        for i in range(4))
            assert total >= 20
            m.close()

    def test_parallel_mixed_rw(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(Config(db_path=os.path.join(d, "c.db")))
            m.add("I work at Google. I live in Paris.", user_id="u")
            errors = []

            def reader():
                try:
                    for _ in range(10):
                        m.search("Where does the user work?", user_id="u")
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            def writer(i):
                try:
                    for j in range(5):
                        m.add(f"Extra fact {i}-{j} about user.",
                              user_id="u")
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = ([threading.Thread(target=reader) for _ in range(3)] +
                       [threading.Thread(target=writer, args=(i,))
                        for i in range(2)])
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors
            m.close()
