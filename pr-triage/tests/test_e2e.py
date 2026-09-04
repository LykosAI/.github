"""End to end: the real script as a subprocess against a fake GitHub API and a fake
OpenAI-compatible server on localhost. Exercises approve, human, outside, offline
and the comment upsert, asserting on the requests the script actually sent.

Run from the repo root: python3 -m unittest discover -s pr-triage/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "triage.py")
SIGNATURE = "🐾 Lykos Pup (fable-fusion on Freya)"
BOT = {"login": "github-actions[bot]", "type": "Bot"}


class FakeState:
    def __init__(self):
        self.calls = []  # (method, path, body_dict_or_None)
        self.model_reply = None  # str content, or Exception subclass to raise, or dict for raw payload
        self.model_status = 200
        self.model_mode = "json"  # json | redirect | html
        self.files = [{"filename": "docs/architecture.md", "status": "modified"}]
        self.diff = "--- a/docs/architecture.md\n+++ b/docs/architecture.md\n@@ -1 +1 @@\n-old\n+new\n"
        self.permission = "admin"
        self.comments = []
        self.reviews = []
        self.model_headers = {}


class Handler(BaseHTTPRequestHandler):
    state: FakeState = None

    def log_message(self, *args):  # keep test output quiet
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else None

    def _send(self, status, payload, content_type="application/json"):
        data = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_PUT(self):
        self._route("PUT")

    def _route(self, method):
        s = self.state
        path = self.path.split("?", 1)[0]
        body = self._body()
        s.calls.append((method, path, body))

        if path == "/v1/chat/completions":
            s.model_headers = {k.lower(): v for k, v in self.headers.items()}
            if s.model_mode == "redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/cdn-cgi/access/login/llm.ionite.io?kid=abc")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if s.model_mode == "html":
                page = "<!DOCTYPE html><html><head><title>Sign in · Cloudflare Access</title></head><body>" + "login " * 40 + "</body></html>"
                assert len(page) > 200  # the 120-char cap must be measurable
                return self._send(200, page, "text/html; charset=utf-8")
            if s.model_status != 200:
                return self._send(s.model_status, {"error": "nope"})
            if isinstance(s.model_reply, dict):
                return self._send(200, s.model_reply)
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": s.model_reply}}]})

        if path.startswith("/cdn-cgi/access/login"):
            return self._send(200, "<html>followed the redirect</html>", "text/html")

        prefix = "/github/repos/LykosAI/Test"
        if not path.startswith(prefix):
            return self._send(404, {"message": "unknown"})
        rest = path[len(prefix):]

        if rest.startswith("/collaborators/") and rest.endswith("/permission"):
            if s.permission is None:
                return self._send(404, {"message": "Not Found"})
            return self._send(200, {"permission": s.permission})
        if rest == "/pulls/7/files":
            return self._send(200, s.files)
        if rest == "/pulls/7":
            if "diff" in (self.headers.get("Accept") or ""):
                return self._send(200, s.diff, "text/plain")
            return self._send(200, {"number": 7})
        if rest == "/issues/7/comments" and method == "GET":
            return self._send(200, s.comments)
        if rest == "/issues/7/comments" and method == "POST":
            comment = {"id": 100 + len(s.comments), "user": BOT, "body": body["body"]}
            s.comments.append(comment)
            return self._send(201, comment)
        if rest.startswith("/issues/comments/") and method == "PATCH":
            cid = int(rest.rsplit("/", 1)[1])
            for c in s.comments:
                if c["id"] == cid:
                    c["body"] = body["body"]
                    return self._send(200, c)
            return self._send(404, {"message": "no such comment"})
        if rest == "/pulls/7/reviews" and method == "GET":
            return self._send(200, s.reviews)
        if rest == "/pulls/7/reviews" and method == "POST":
            review = {"id": 500 + len(s.reviews), "user": BOT, "state": "APPROVED", "body": body["body"]}
            s.reviews.append(review)
            return self._send(200, review)
        if rest.startswith("/pulls/7/reviews/") and rest.endswith("/dismissals") and method == "PUT":
            rid = int(rest.split("/")[4])
            for r in s.reviews:
                if r["id"] == rid:
                    r["state"] = "DISMISSED"
                    return self._send(200, r)
            return self._send(404, {"message": "no such review"})
        return self._send(404, {"message": f"unrouted {method} {rest}"})


class E2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = FakeState()
        Handler.state = cls.state
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.state.__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.event_path = os.path.join(self.tmp.name, "event.json")
        self.write_event()

    def tearDown(self):
        self.tmp.cleanup()

    def write_event(self, association="MEMBER", login="ionite34"):
        event = {
            "action": "synchronize",
            "pull_request": {
                "number": 7,
                "title": "docs: clarify the envelope",
                "body": "Just words.",
                "author_association": association,
                "user": {"login": login, "type": "User"},
                "head": {"sha": "0123456789abcdef"},
                "base": {"ref": "main"},
            },
        }
        with open(self.event_path, "w", encoding="utf-8") as f:
            json.dump(event, f)

    def run_script(self, endpoint=None, envelope="docs/**\n**/*.md"):
        env = dict(
            os.environ,
            GITHUB_EVENT_PATH=self.event_path,
            GITHUB_REPOSITORY="LykosAI/Test",
            GITHUB_API_URL=f"http://127.0.0.1:{self.port}/github",
            GITHUB_TOKEN="ghs_fake",
            TRIAGE_ENVELOPE=envelope,
            TRIAGE_MODEL="fake-model",
            TRIAGE_ENDPOINT=endpoint or f"http://127.0.0.1:{self.port}/v1",
            TRIAGE_SIGNATURE=SIGNATURE,
            CF_ACCESS_CLIENT_ID="cf-id-value",
            CF_ACCESS_CLIENT_SECRET="cf-secret-value",
            LLM_API_KEY="llm-key-value",
            PYTHONIOENCODING="utf-8",
        )
        return subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)

    def calls(self, method, suffix):
        return [c for c in self.state.calls if c[0] == method and c[1].endswith(suffix)]

    def test_approve_inside_envelope_posts_an_approving_review_and_no_comment(self):
        self.state.model_reply = '{"verdict": "approve", "reason": "tidy docs :3", "notes": "nothing rattling owo"}'
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        reviews = self.calls("POST", "/pulls/7/reviews")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0][2]["event"], "APPROVE")
        self.assertEqual(reviews[0][2]["commit_id"], "0123456789abcdef")
        self.assertIn("tidy docs :3", reviews[0][2]["body"])
        self.assertIn("nothing rattling owo", reviews[0][2]["body"])
        self.assertTrue(reviews[0][2]["body"].endswith(SIGNATURE))
        self.assertEqual(self.calls("POST", "/issues/7/comments"), [])
        # the model call carried the auth headers and never leaked into the log
        self.assertEqual(self.state.model_headers.get("cf-access-client-id"), "cf-id-value")
        self.assertEqual(self.state.model_headers.get("cf-access-client-secret"), "cf-secret-value")
        self.assertEqual(self.state.model_headers.get("authorization"), "Bearer llm-key-value")
        for secret in ("cf-id-value", "cf-secret-value", "llm-key-value"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_widened_envelope_approves_a_non_markdown_path_and_tells_the_model(self):
        self.state.files = [{"filename": "Lykos.Chat.Core.csproj", "status": "modified"}]
        self.state.diff = "--- a/Lykos.Chat.Core.csproj\n+++ b/Lykos.Chat.Core.csproj\n@@ -1 +1 @@\n-<Version>0.29.0</Version>\n+<Version>0.30.0</Version>\n"
        self.state.model_reply = '{"verdict": "approve", "reason": "a plain version bump :bcnod:"}'
        result = self.run_script(envelope="docs/**\n**/*.md\nLykos.Chat.Core.csproj")
        self.assertEqual(result.returncode, 0, result.stderr)
        reviews = self.calls("POST", "/pulls/7/reviews")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0][2]["event"], "APPROVE")
        system = self.calls("POST", "/v1/chat/completions")[0][2]["messages"][0]["content"]
        self.assertIn("`Lykos.Chat.Core.csproj`", system)
        self.assertIn("`.github/`", system)

    def test_default_envelope_still_refuses_the_same_csproj_path(self):
        self.state.files = [{"filename": "Lykos.Chat.Core.csproj", "status": "modified"}]
        self.state.model_reply = '{"verdict": "approve", "reason": "a plain version bump :bcnod:"}'
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        self.assertEqual(len(self.calls("POST", "/issues/7/comments")), 1)

    def test_model_request_shape(self):
        self.state.model_reply = '{"verdict": "human", "reason": "hm"}'
        self.run_script()
        model_calls = self.calls("POST", "/v1/chat/completions")
        self.assertEqual(len(model_calls), 1)
        payload = model_calls[0][2]
        self.assertEqual(payload["model"], "fake-model")
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        user = payload["messages"][1]["content"]
        self.assertIn("docs: clarify the envelope", user)
        self.assertIn("docs/architecture.md", user)
        self.assertIn("+new", user)

    def test_human_verdict_posts_one_comment_and_edits_it_on_the_next_run(self):
        self.state.model_reply = '{"verdict": "human", "reason": "one line reads odd :firHmm:", "notes": "line 3 maybe?"}'
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        posted = self.calls("POST", "/issues/7/comments")
        self.assertEqual(len(posted), 1)
        body = posted[0][2]["body"]
        self.assertIn("<!-- lykos-pr-triage -->", body)
        self.assertIn("one line reads odd :firHmm:", body)
        self.assertIn("line 3 maybe?", body)
        self.assertIn("human should look", body.lower())
        self.assertTrue(body.endswith(SIGNATURE))

        self.state.calls.clear()
        self.state.model_reply = '{"verdict": "human", "reason": "still odd"}'
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls("POST", "/issues/7/comments"), [])
        patched = self.calls("PATCH", "/issues/comments/100")
        self.assertEqual(len(patched), 1)
        self.assertIn("still odd", patched[0][2]["body"])

    def test_outside_envelope_never_approves_and_only_comments(self):
        self.state.files = [{"filename": ".github/workflows/pr-triage.yml", "status": "added"}]
        self.state.model_reply = '{"verdict": "approve", "reason": "looks fine to me!"}'
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        posted = self.calls("POST", "/issues/7/comments")
        self.assertEqual(len(posted), 1)
        body = posted[0][2]["body"]
        self.assertIn(".github/workflows/pr-triage.yml", body)
        self.assertIn("looks fine to me!", body)
        self.assertIn("not something I may approve", body)

    def test_non_member_never_approves(self):
        self.write_event(association="CONTRIBUTOR", login="stranger")
        self.state.permission = None
        self.state.model_reply = '{"verdict": "approve", "reason": "sure"}'
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        self.assertEqual(len(self.calls("POST", "/issues/7/comments")), 1)

    def test_a_stale_approval_is_dismissed_when_the_next_push_is_not_approvable(self):
        self.state.reviews = [
            {"id": 500, "user": BOT, "state": "APPROVED", "body": "<!-- lykos-pr-triage -->\nold"},
            {"id": 501, "user": {"login": "mohnjiles", "type": "User"}, "state": "APPROVED", "body": "lgtm"},
        ]
        self.state.files = [{"filename": "Services/ChatBrain.cs", "status": "modified"}]
        self.state.model_reply = '{"verdict": "approve", "reason": "sure"}'
        self.assertEqual(self.run_script().returncode, 0)
        dismissals = self.calls("PUT", "/dismissals")
        self.assertEqual([c[1] for c in dismissals], ["/github/repos/LykosAI/Test/pulls/7/reviews/500/dismissals"])
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])

    def test_offline_model_posts_the_offline_comment_and_exits_zero(self):
        result = self.run_script(endpoint="http://127.0.0.1:1/v1")  # nothing listens here
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        posted = self.calls("POST", "/issues/7/comments")
        self.assertEqual(len(posted), 1)
        self.assertIn("could not reach my model", posted[0][2]["body"])
        self.assertIn("::warning::", result.stdout)

    def test_model_http_error_is_the_offline_path(self):
        self.state.model_status = 502
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        self.assertIn("could not reach my model", self.calls("POST", "/issues/7/comments")[0][2]["body"])

    def test_garbage_reply_is_the_offline_path(self):
        self.state.model_reply = "I have thought about it and I APPROVE wholeheartedly."
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        self.assertIn("could not reach my model", self.calls("POST", "/issues/7/comments")[0][2]["body"])

    def test_empty_content_with_only_reasoning_is_the_offline_path(self):
        self.state.model_reply = {"choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "thinking..."}}]}
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        self.assertEqual(len(self.calls("POST", "/issues/7/comments")), 1)

    def test_access_redirect_is_reported_as_a_302_and_never_followed(self):
        self.state.model_mode = "redirect"
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        self.assertIn("could not reach my model", self.calls("POST", "/issues/7/comments")[0][2]["body"])
        self.assertEqual(len(self.calls("POST", "/v1/chat/completions")), 1)
        self.assertEqual([c for c in self.state.calls if c[1].startswith("/cdn-cgi/")], [])
        warning = [l for l in result.stdout.splitlines() if "::warning::Model unreachable" in l][0]
        self.assertIn("ModelReplyError", warning)
        self.assertIn("HTTP 302 redirect to 127.0.0.1", warning)
        self.assertNotIn("kid=abc", warning)
        for secret in ("cf-id-value", "cf-secret-value", "llm-key-value"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_html_200_is_reported_with_status_url_content_type_and_capped_body(self):
        self.state.model_mode = "html"
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])
        self.assertIn("could not reach my model", self.calls("POST", "/issues/7/comments")[0][2]["body"])
        warning = [l for l in result.stdout.splitlines() if "::warning::Model unreachable" in l][0]
        self.assertIn("non-JSON reply: HTTP 200 from http://127.0.0.1:", warning)
        self.assertIn("/v1/chat/completions", warning)
        self.assertIn("Content-Type text/html; charset=utf-8", warning)
        self.assertIn("<!DOCTYPE html><html><head><title>Sign in", warning)
        self.assertNotIn("</html>", warning)  # capped well before the end of the body
        for secret in ("cf-id-value", "cf-secret-value", "llm-key-value"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_credential_presence_line_carries_booleans_and_lengths_only(self):
        self.state.model_reply = '{"verdict": "human", "reason": "hm"}'
        result = self.run_script()
        line = [l for l in result.stdout.splitlines() if l.startswith("Model credentials in the environment:")][0]
        self.assertIn("CF_ACCESS_CLIENT_ID present=True len=11", line)
        self.assertIn("CF_ACCESS_CLIENT_SECRET present=True len=15", line)
        self.assertIn("LLM_API_KEY present=True len=13", line)
        for secret in ("cf-id-value", "cf-secret-value", "llm-key-value"):
            self.assertNotIn(secret, line)

    def test_missing_credentials_show_as_absent(self):
        self.state.model_reply = '{"verdict": "human", "reason": "hm"}'
        env = dict(
            os.environ,
            GITHUB_EVENT_PATH=self.event_path,
            GITHUB_REPOSITORY="LykosAI/Test",
            GITHUB_API_URL=f"http://127.0.0.1:{self.port}/github",
            GITHUB_TOKEN="ghs_fake",
            TRIAGE_MODEL="fake-model",
            TRIAGE_ENDPOINT=f"http://127.0.0.1:{self.port}/v1",
            CF_ACCESS_CLIENT_ID="",
            PYTHONIOENCODING="utf-8",
        )
        env.pop("CF_ACCESS_CLIENT_SECRET", None)
        env.pop("LLM_API_KEY", None)
        result = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        line = [l for l in result.stdout.splitlines() if l.startswith("Model credentials in the environment:")][0]
        self.assertIn("CF_ACCESS_CLIENT_ID present=False len=0", line)
        self.assertIn("CF_ACCESS_CLIENT_SECRET present=False len=0", line)
        self.assertIn("LLM_API_KEY present=False len=0", line)

    def test_ask_model_raises_a_diagnosable_error_on_redirect_and_html(self):
        sys.path.insert(0, os.path.join(HERE, ".."))
        import triage

        endpoint = f"http://127.0.0.1:{self.port}/v1"
        self.state.model_mode = "redirect"
        with self.assertRaises(triage.ModelReplyError) as ctx:
            triage.ask_model(endpoint, "m", "k", "i", "s", "sys", "user")
        self.assertIn("HTTP 302 redirect to 127.0.0.1", str(ctx.exception))
        self.state.model_mode = "html"
        with self.assertRaises(triage.ModelReplyError) as ctx:
            triage.ask_model(endpoint, "m", "k", "i", "s", "sys", "user")
        self.assertIn("non-JSON reply: HTTP 200", str(ctx.exception))
        self.assertIn("text/html", str(ctx.exception))

    def test_github_outage_exits_zero(self):
        self.state.model_reply = '{"verdict": "approve", "reason": "ok"}'
        env_override = f"http://127.0.0.1:{self.port}/nowhere"
        env = dict(
            os.environ,
            GITHUB_EVENT_PATH=self.event_path,
            GITHUB_REPOSITORY="LykosAI/Test",
            GITHUB_API_URL=env_override,
            GITHUB_TOKEN="ghs_fake",
            TRIAGE_MODEL="fake-model",
            TRIAGE_ENDPOINT=f"http://127.0.0.1:{self.port}/v1",
            PYTHONIOENCODING="utf-8",
        )
        result = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::", result.stdout)
        self.assertEqual(self.calls("POST", "/pulls/7/reviews"), [])


if __name__ == "__main__":
    unittest.main()
