import unittest

from bs4 import BeautifulSoup

from vos import VOSClient


class VOSUserOptionsTest(unittest.TestCase):
    def test_extract_checkbox_users(self):
        soup = BeautifulSoup(
            """
            <div id="m_box_custFirstUserId">
              <input type="checkbox" name="custFirstUserId" value="33" />水本勇樹<br/>
              <input type="checkbox" name="custFirstUserId" value="451" />SDBDY07029<br/>
            </div>
            <div id="m_box_custSecondUserId">
              <input type="checkbox" name="custSecondUserId" value="33" />水本勇樹<br/>
              <input type="checkbox" name="custSecondUserId" value="999" />新担当者<br/>
            </div>
            """,
            "html.parser",
        )

        users = VOSClient._extract_checkbox_users(
            soup,
            ("custFirstUserId", "custSecondUserId"),
        )

        self.assertEqual(
            users,
            [
                {"id": "33", "name": "水本勇樹"},
                {"id": "451", "name": "SDBDY07029"},
                {"id": "999", "name": "新担当者"},
            ],
        )


class UsersApiTest(unittest.TestCase):
    def test_api_users_returns_vos_users(self):
        try:
            import app as app_module
        except ModuleNotFoundError as exc:
            if exc.name == "flask":
                self.skipTest("Flask is not installed in this Python environment")
            raise

        original = app_module.VOSClient

        class FakeVOS:
            def __init__(self, login_id, password):
                self.login_id = login_id
                self.password = password

            def get_user_options(self):
                return [{"id": "1", "name": "担当者A"}]

        try:
            app_module.VOSClient = FakeVOS
            client = app_module.app.test_client()
            response = client.post(
                "/api/users",
                json={"loginId": "user", "password": "pass"},
            )
        finally:
            app_module.VOSClient = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"users": [{"id": "1", "name": "担当者A"}]})


if __name__ == "__main__":
    unittest.main()
