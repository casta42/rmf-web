"""F4.2 (FR-30): GET /health says which release is running, to anyone;
GET /about gives the full build identity, to a signed-in user only.

Both ways: the unauthenticated probe carries the version and NOTHING that
fingerprints the build (commit, pin, patches); /about refuses a request
with no identity and answers a signed-in one with all of it — the same
values build_identity() reads from the image."""

from api_server.build_identity import build_identity
from api_server.test import AppFixture


class TestHealthAndAbout(AppFixture):
    def test_health_names_the_release_without_signing_in(self):
        resp = self.client.get("/health", headers={"Authorization": ""})
        self.assertEqual(200, resp.status_code)
        body = resp.json()
        self.assertEqual("ok", body["status"])
        self.assertEqual(build_identity()["version"], body["version"])

    def test_health_carries_nothing_that_fingerprints_the_build(self):
        body = self.client.get("/health", headers={"Authorization": ""}).json()
        self.assertEqual({"status", "version"}, set(body))

    def test_about_refuses_a_request_with_no_identity(self):
        resp = self.client.get("/about", headers={"Authorization": ""})
        self.assertIn(resp.status_code, (401, 403))

    def test_about_gives_a_signed_in_user_the_whole_identity(self):
        resp = self.client.get("/about")
        self.assertEqual(200, resp.status_code)
        self.assertEqual(build_identity(), resp.json())
