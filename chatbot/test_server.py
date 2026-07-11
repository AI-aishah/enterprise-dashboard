import unittest
import shutil
import tempfile
from io import BytesIO
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

import init_db
import server


class WorkbookQueryTests(unittest.TestCase):
    def test_hash_password_uses_bcrypt_and_verifies_password(self):
        password_hash = server.hash_password("correct horse battery staple")
        self.assertTrue(password_hash.startswith("$2b$"))
        self.assertTrue(server.verify_password("correct horse battery staple", password_hash))
        self.assertFalse(server.verify_password("wrong password", password_hash))

    def test_hash_password_rejects_empty_password(self):
        with self.assertRaises(ValueError):
            server.hash_password("")

    def test_verify_password_rejects_invalid_hash(self):
        self.assertFalse(server.verify_password("password", "not-a-bcrypt-hash"))

    def test_generate_otp_returns_six_digits(self):
        otp = server.generate_otp()
        self.assertEqual(len(otp), server.OTP_LENGTH)
        self.assertTrue(otp.isdigit())

    @patch.dict("server.os.environ", {"GMAIL_ADDRESS": "sender@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    def test_gmail_credentials_strip_app_password_spaces(self):
        self.assertEqual(server.gmail_credentials(), ("sender@gmail.com", "abcdefghijklmnop"))

    @patch("server.send_otp_email")
    @patch("server.generate_otp", return_value="123456")
    def test_request_otp_stores_hashed_code_and_sends_email(self, generate_otp, send_email):
        server.OTP_STORE.clear()
        server.request_otp("USER@Example.COM")
        send_email.assert_called_once_with("user@example.com", "123456")
        record = server.OTP_STORE["user@example.com"]
        self.assertNotEqual(record["hash"], "123456")
        self.assertTrue(server.verify_password("123456", record["hash"]))

    def test_verify_otp_consumes_valid_code(self):
        server.OTP_STORE.clear()
        server.store_otp("user@example.com", "123456")
        self.assertTrue(server.verify_otp("user@example.com", "123456"))
        self.assertNotIn("user@example.com", server.OTP_STORE)

    def test_verify_otp_rejects_wrong_or_expired_code(self):
        server.OTP_STORE.clear()
        server.store_otp("user@example.com", "123456")
        self.assertFalse(server.verify_otp("user@example.com", "000000"))
        server.OTP_STORE["user@example.com"]["expires_at"] = 0
        self.assertFalse(server.verify_otp("user@example.com", "123456"))
        self.assertNotIn("user@example.com", server.OTP_STORE)

    def delete_auth_user(self, email):
        server.ensure_auth_database()
        with closing(server.open_auth_database()) as connection:
            connection.execute("DELETE FROM users WHERE email = ?", (email,))
            connection.commit()

    @patch("server.send_otp_email")
    @patch("server.generate_otp", return_value="123456")
    def test_signup_requires_otp_before_login(self, generate_otp, send_email):
        self.delete_auth_user("fatimah.alzahrani@example-company.com")
        server.OTP_STORE.clear()
        server.signup_user("Fatimah Alzahrani", "fatimah.alzahrani@example-company.com", "secure-password")
        self.assertIsNone(server.authenticate_user("fatimah.alzahrani@example-company.com", "secure-password"))
        send_email.assert_called_once_with("fatimah.alzahrani@example-company.com", "123456")
        self.assertTrue(server.verify_signup_otp("fatimah.alzahrani@example-company.com", "123456"))
        user = server.authenticate_user("fatimah.alzahrani@example-company.com", "secure-password")
        self.assertIsNotNone(user)
        expected = server.signup_profile_from_workbook("Fatimah Alzahrani", "fatimah.alzahrani@example-company.com")
        self.assertEqual(user["department"], expected["department"])
        self.assertEqual(user["role"], expected["role"])
        self.delete_auth_user("fatimah.alzahrani@example-company.com")

    def test_signup_rejects_placeholder_email_domains(self):
        with self.assertRaisesRegex(ValueError, "real email address"):
            server.signup_user("Aishah", "aishah@example.com", "secure-password")

    @patch.object(server, "AISHAH_ADMIN_EMAILS", {"aishah.test@company.com"})
    @patch("server.send_otp_email")
    @patch("server.generate_otp", return_value="123456")
    def test_aishah_signup_is_forced_to_admin(self, generate_otp, send_email):
        self.delete_auth_user("aishah.test@company.com")
        server.OTP_STORE.clear()
        server.signup_user("AishahAbunaja", "aishah.test@company.com", "secure-password")
        self.assertTrue(server.verify_signup_otp("aishah.test@company.com", "123456"))
        user = server.authenticate_user("aishah.test@company.com", "secure-password")
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "Administrator")
        self.delete_auth_user("aishah.test@company.com")

    def test_signup_rejects_people_missing_from_employee_sheet(self):
        self.delete_auth_user("not.in.workbook@company.com")
        with self.assertRaisesRegex(ValueError, "employee record was not found"):
            server.signup_user("Missing Person", "not.in.workbook@company.com", "secure-password")

    def test_role_navigation_rules_hide_disallowed_pages(self):
        self.assertIn("activity-log.html", server.allowed_pages({"role": "Administrator"}))
        self.assertNotIn("activity-log.html", server.allowed_pages({"role": "HR Manager"}))
        self.assertNotIn("employees.html", server.allowed_pages({"role": "Project Manager"}))
        self.assertIn("employees.html", server.allowed_pages({"role": "Employee"}))

    def test_department_director_rows_are_scoped_to_department(self):
        user = {"email": "director@example.com", "name": "Director", "department": "Finance", "role": "Department Director"}
        table = server.fetch_table("data_tasks", user)
        self.assertGreater(table["row_count"], 0)
        self.assertTrue(all(row["department"] == "Finance" for row in table["rows"]))

    def test_project_manager_cannot_read_hidden_tables(self):
        user = {"email": "pm@example.com", "name": "PM", "department": "Technology", "role": "Project Manager"}
        table = server.fetch_table("data_employees", user)
        self.assertEqual(table["row_count"], 0)

    def test_employee_rows_are_scoped_to_own_records(self):
        user = {
            "email": "fatimah.alzahrani@example-company.com",
            "name": "Fatimah Alzahrani",
            "department": "Executive Office",
            "role": "Employee",
        }
        employees = server.fetch_table("data_employees", user)
        self.assertEqual(employees["row_count"], 1)
        self.assertEqual(employees["rows"][0]["email"], user["email"])
        tasks = server.fetch_table("data_tasks", user)
        self.assertTrue(all(row["assigned_to"] == user["name"] for row in tasks["rows"]))

    def test_login_missing_account_returns_create_account_feedback(self):
        self.delete_auth_user("missing.user@example.com")
        handler = server.DashboardHandler.__new__(server.DashboardHandler)
        handler.path = "/auth/login"
        handler.read_json_body = MagicMock(return_value={
            "email": "missing.user@example.com",
            "password": "secure-password",
        })
        handler.send_json = MagicMock()

        handler.do_POST()

        handler.send_json.assert_called_once_with(
            404,
            {"error": "There's no account with this email. Create an account first."},
        )

    @patch("server.send_otp_email")
    @patch("server.generate_otp", return_value="123456")
    def test_password_reset_updates_password_after_otp(self, generate_otp, send_email):
        self.delete_auth_user("reset.user@example.com")
        server.OTP_STORE.clear()
        server.create_user("Reset User", "reset.user@example.com", server.hash_password("old-password"), "Technology")

        server.request_password_reset("reset.user@example.com")
        send_email.assert_called_once_with("reset.user@example.com", "123456")
        self.assertFalse(server.reset_password("reset.user@example.com", "000000", "new-password"))
        self.assertTrue(server.reset_password("reset.user@example.com", "123456", "new-password"))

        self.assertIsNone(server.authenticate_user("reset.user@example.com", "old-password"))
        self.assertIsNotNone(server.authenticate_user("reset.user@example.com", "new-password"))
        self.assertNotIn("reset.user@example.com", server.OTP_STORE)
        self.delete_auth_user("reset.user@example.com")

    def test_password_reset_rejects_missing_account(self):
        self.delete_auth_user("no.reset@example.com")
        with self.assertRaisesRegex(ValueError, "no account"):
            server.request_password_reset("no.reset@example.com")

    def test_session_round_trip_and_destroy(self):
        self.delete_auth_user("session.user@example.com")
        server.create_user("Session User", "session.user@example.com", server.hash_password("secure-password"), "Technology")
        token = server.create_session("session.user@example.com")
        self.assertEqual(server.session_user(token)["email"], "session.user@example.com")
        server.destroy_session(token)
        self.assertIsNone(server.session_user(token))
        self.delete_auth_user("session.user@example.com")

    @patch.dict("server.os.environ", {"SESSION_SECRET": "test-shared-secret"})
    def test_signed_session_survives_without_in_memory_state(self):
        self.delete_auth_user("signed.user@example.com")
        server.create_user("Signed User", "signed.user@example.com", server.hash_password("secure-password"), "Technology")
        token = server.create_session("signed.user@example.com")
        server.SESSIONS.clear()

        user = server.session_user(token)
        self.assertEqual(user["email"], "signed.user@example.com")
        self.assertEqual(user["department"], "Technology")
        self.assertIsNone(server.session_user(token + "tampered"))
        self.delete_auth_user("signed.user@example.com")

    @patch.dict("server.os.environ", {"SESSION_SECRET": "test-shared-secret"})
    def test_signed_admin_session_can_be_unlocked(self):
        self.delete_auth_user("signed.admin@example.com")
        server.create_user("Signed Admin", "signed.admin@example.com", server.hash_password("secure-password"), "Technology", "Administrator")
        token = server.create_session("signed.admin@example.com")

        elevated = server.elevated_session_token(token, "20032003")
        self.assertIsNotNone(elevated)
        self.assertTrue(server.session_user(elevated)["admin_unlocked"])
        self.assertIsNone(server.elevated_session_token(token, "wrong-password"))
        self.delete_auth_user("signed.admin@example.com")

    def test_admin_unlock_sets_session_flag_only_with_password(self):
        self.delete_auth_user("admin.unlock@example.com")
        server.create_user("Admin Unlock", "admin.unlock@example.com", server.hash_password("secure-password"), "Technology", "Administrator")
        token = server.create_session("admin.unlock@example.com")

        self.assertFalse(server.unlock_admin_session(token, "wrong-password"))
        self.assertFalse(server.session_user(token)["admin_unlocked"])
        self.assertTrue(server.unlock_admin_session(token, "20032003"))
        self.assertTrue(server.session_user(token)["admin_unlocked"])
        self.delete_auth_user("admin.unlock@example.com")

    def test_admin_user_management_helpers_create_update_promote_delete(self):
        self.delete_auth_user("managed.user@example.com")

        server.create_user_with_password(
            "Managed User",
            "managed.user@example.com",
            "secure-password",
            "Technology",
            "Employee",
        )
        user = server.authenticate_user("managed.user@example.com", "secure-password")
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "Employee")

        server.update_user_record(
            "managed.user@example.com",
            "Managed User Updated",
            "Finance",
            "Project Manager",
            "new-secure-password",
        )
        self.assertIsNone(server.authenticate_user("managed.user@example.com", "secure-password"))
        user = server.authenticate_user("managed.user@example.com", "new-secure-password")
        self.assertEqual(user["name"], "Managed User Updated")
        self.assertEqual(user["department"], "Finance")
        self.assertEqual(user["role"], "Project Manager")

        server.change_user_role("managed.user@example.com", "Administrator")
        self.assertEqual(server.user_by_email("managed.user@example.com")["role"], "Administrator")

        server.delete_user_record("managed.user@example.com")
        self.assertIsNone(server.user_by_email("managed.user@example.com"))

    def test_admin_user_management_rejects_missing_user(self):
        self.delete_auth_user("missing.admin.user@example.com")
        with self.assertRaisesRegex(ValueError, "User not found"):
            server.update_user_record("missing.admin.user@example.com", "Missing", "Technology", "Employee")
        with self.assertRaisesRegex(ValueError, "User not found"):
            server.change_user_role("missing.admin.user@example.com", "Administrator")
        with self.assertRaisesRegex(ValueError, "User not found"):
            server.delete_user_record("missing.admin.user@example.com")

    def test_sync_employee_workbook_create_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "sample_data.xlsx"
            shutil.copy2(server.WORKBOOK_PATH, workbook)

            init_db.sync_employee_workbook(
                workbook,
                action="create",
                employee={
                    "employee_name": "Workbook Sync User",
                    "email": "workbook.sync@example.com",
                    "department": "Corporate Training",
                    "role": "Employee",
                },
            )
            rows = init_db.read_workbook(workbook)["Employees"]
            self.assertTrue(any(row.get("Email") == "workbook.sync@example.com" for row in rows))

            init_db.sync_employee_workbook(
                workbook,
                action="delete",
                employee={"email": "workbook.sync@example.com"},
            )
            rows = init_db.read_workbook(workbook)["Employees"]
            self.assertFalse(any(row.get("Email") == "workbook.sync@example.com" for row in rows))

    @patch("server.ensure_database_current")
    @patch("server.delete_user_record")
    @patch("server.sync_employee_workbook", side_effect=ValueError("User not found"))
    @patch("server.user_by_email", return_value={"email": "stale.user@example.com"})
    @patch("server.session_record", return_value={"admin_unlocked": True})
    @patch("server.DashboardHandler.current_user", return_value={"role": "Administrator"})
    def test_admin_delete_removes_auth_user_when_workbook_row_is_missing(
        self,
        current_user,
        session_record,
        user_by_email,
        sync_employee_workbook,
        delete_user_record,
        ensure_database_current,
    ):
        handler = server.DashboardHandler.__new__(server.DashboardHandler)
        handler.path = "/admin/users"
        handler.session_token = MagicMock(return_value="token")
        handler.read_json_body = MagicMock(return_value={
            "action": "delete",
            "email": "stale.user@example.com",
        })
        handler.send_json = MagicMock()

        handler.do_POST()

        delete_user_record.assert_called_once_with("stale.user@example.com")
        handler.send_json.assert_called_once_with(200, {"ok": True})

    @patch("server.ensure_database_current")
    @patch("server.change_user_role")
    @patch("server.sync_employee_workbook")
    @patch("server.user_by_email", return_value=None)
    @patch("server.session_record", return_value={"admin_unlocked": True})
    @patch("server.DashboardHandler.current_user", return_value={"role": "Administrator"})
    def test_admin_make_admin_does_not_require_login_account(
        self,
        current_user,
        session_record,
        user_by_email,
        sync_employee_workbook,
        change_user_role,
        ensure_database_current,
    ):
        handler = server.DashboardHandler.__new__(server.DashboardHandler)
        handler.path = "/admin/users"
        handler.session_token = MagicMock(return_value="token")
        handler.read_json_body = MagicMock(return_value={
            "action": "make_admin",
            "email": "employee.only@example.com",
            "name": "Employee Only",
            "department": "Technology",
        })
        handler.send_json = MagicMock()

        handler.do_POST()

        sync_employee_workbook.assert_called_once_with(
            server.WORKBOOK_PATH,
            action="promote",
            employee={"email": "employee.only@example.com", "role": "Administrator"},
        )
        change_user_role.assert_not_called()
        handler.send_json.assert_called_once_with(200, {"ok": True})

    @patch("server.ensure_database_current")
    @patch("server.change_user_role")
    @patch("server.sync_employee_workbook")
    @patch("server.user_by_email", return_value={"email": "admin.user@example.com"})
    @patch("server.session_record", return_value={"admin_unlocked": True})
    @patch("server.DashboardHandler.current_user", return_value={"role": "Administrator"})
    def test_admin_remove_admin_downgrades_workbook_and_login_account(
        self,
        current_user,
        session_record,
        user_by_email,
        sync_employee_workbook,
        change_user_role,
        ensure_database_current,
    ):
        handler = server.DashboardHandler.__new__(server.DashboardHandler)
        handler.path = "/admin/users"
        handler.session_token = MagicMock(return_value="token")
        handler.read_json_body = MagicMock(return_value={
            "action": "remove_admin",
            "email": "admin.user@example.com",
            "name": "Admin User",
            "department": "Technology",
        })
        handler.send_json = MagicMock()

        handler.do_POST()

        _, kwargs = sync_employee_workbook.call_args
        self.assertEqual(kwargs["action"], "update")
        self.assertEqual(kwargs["employee"]["job_title"], "Employee")
        self.assertEqual(kwargs["employee"]["level"], "Employee")
        change_user_role.assert_called_once_with("admin.user@example.com", "Employee")
        handler.send_json.assert_called_once_with(200, {"ok": True})

    def test_logout_post_destroys_session_and_redirects(self):
        token = server.create_session("logout.user@example.com")
        handler = server.DashboardHandler.__new__(server.DashboardHandler)
        handler.path = "/auth/logout"
        handler.headers = {"Cookie": f"{server.SESSION_COOKIE}={token}"}
        handler.send_logout_redirect = MagicMock()

        handler.do_POST()

        self.assertIsNone(server.session_user(token))
        handler.send_logout_redirect.assert_called_once_with()

    def test_logout_redirect_clears_cookie_and_points_to_login(self):
        handler = server.DashboardHandler.__new__(server.DashboardHandler)
        calls = []
        handler.send_response = lambda status: calls.append(("status", status))
        handler.send_header = lambda name, value: calls.append(("header", name, value))
        handler.send_cors_headers = lambda: calls.append(("cors",))
        handler.end_headers = lambda: calls.append(("end",))

        handler.send_logout_redirect()

        self.assertIn(("status", 303), calls)
        self.assertIn(("header", "Location", "/login.html"), calls)
        self.assertIn(("header", "Content-Length", "0"), calls)
        cookie_headers = [call[2] for call in calls if call[:2] == ("header", "Set-Cookie")]
        self.assertEqual(len(cookie_headers), 1)
        self.assertIn(f"{server.SESSION_COOKIE}=;", cookie_headers[0])
        self.assertIn("Max-Age=0", cookie_headers[0])

    def test_schema_is_generated_from_database(self):
        schema = server.workbook_schema()
        self.assertIn('CREATE TABLE "data_employees"', schema)
        self.assertIn('"employee_id" TEXT', schema)

    def test_aggregation_query(self):
        result = server.query_workbook(
            "SELECT COUNT(*) AS blocked FROM data_tasks WHERE status = 'Blocked'"
        )
        self.assertEqual(result["rows"], [[17]])
        self.assertFalse(result["truncated"])

    def test_table_catalog_lists_all_sheets(self):
        catalog = server.table_catalog()
        self.assertIn("data_employees", catalog)
        expected_employees = len(init_db.read_workbook(server.WORKBOOK_PATH)["Employees"])
        self.assertEqual(catalog["data_employees"]["row_count"], expected_employees)
        keys = [c["key"] for c in catalog["data_employees"]["columns"]]
        self.assertIn("employee_id", keys)

    def test_fetch_table_returns_full_rows_unbounded(self):
        result = server.fetch_table("data_activity_log")
        self.assertEqual(result["row_count"], 700)
        self.assertEqual(len(result["rows"]), 700)

    def test_fetch_table_rejects_unknown_table(self):
        self.assertIsNone(server.fetch_table("data_departments; DROP TABLE data_departments"))

    def test_global_search_finds_records_across_tables(self):
        result = server.global_search("Fatimah Alzahrani")
        self.assertGreater(result["total"], 0)
        self.assertTrue(any(item["sheet_name"] == "Employees" for item in result["results"]))

    def test_workbook_freshness_metadata_matches_source(self):
        metadata = server.ensure_database_current()
        self.assertEqual(metadata["source_modified_ns"], str(server.WORKBOOK_PATH.stat().st_mtime_ns))
        payload = server.freshness_payload()
        self.assertTrue(payload["in_sync"])
        self.assertGreater(payload["workbook_rows"], 0)

    def test_write_query_is_rejected(self):
        result = server.query_workbook("DELETE FROM data_tasks")
        self.assertIn("error", result)

    def test_invalid_column_returns_a_tool_error(self):
        result = server.query_workbook("SELECT missing FROM data_tasks")
        self.assertIn("error", result)

    def test_large_query_result_is_bounded(self):
        result = server.query_workbook("SELECT * FROM data_activity_log")
        self.assertLessEqual(result["row_count"], server.MAX_QUERY_ROWS)
        self.assertTrue(result["truncated"])

    @patch("server.time.sleep")
    @patch("server.urlopen")
    def test_temporary_503_is_retried(self, open_url, sleep):
        unavailable = HTTPError(
            server.GEMINI_API_URL, 503, "Unavailable", {}, BytesIO(b"busy")
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"candidates": []}'
        open_url.side_effect = [unavailable, response]
        self.assertEqual(server.gemini_request([], False), {"candidates": []})
        self.assertEqual(open_url.call_count, 2)
        sleep.assert_called_once()

    @patch("server.gemini_request")
    def test_function_call_cycle(self, request):
        request.side_effect = [
            {
                "candidates": [{
                    "content": {
                        "role": "model",
                        "parts": [{
                            "functionCall": {
                                "id": "call-1",
                                "name": "query_workbook",
                                "args": {"sql": "SELECT COUNT(*) AS total FROM data_projects"},
                            }
                        }],
                    }
                }]
            },
            {
                "candidates": [{
                    "content": {
                        "role": "model",
                        "parts": [{"text": "There are 24 projects."}],
                    }
                }]
            },
        ]
        self.assertEqual(server.call_gemini("How many projects?"), "There are 24 projects.")
        second_contents = request.call_args_list[1].args[0]
        function_response = next(
            part["functionResponse"]
            for content in second_contents
            for part in content["parts"]
            if "functionResponse" in part
        )
        self.assertEqual(function_response["id"], "call-1")
        self.assertEqual(function_response["response"]["result"]["rows"], [[24]])


if __name__ == "__main__":
    unittest.main()
