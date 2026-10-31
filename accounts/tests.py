from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse


class AccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "AuditPassword123!"
        cls.user = get_user_model().objects.create_user(
            username="existing_user", password=cls.password
        )

    def test_signup_creates_user_and_logs_in(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "new_user",
                "password1": "AnotherPassword123!",
                "password2": "AnotherPassword123!",
            },
        )

        self.assertRedirects(response, reverse("profile"))
        user = get_user_model().objects.get(username="new_user")
        self.assertTrue(user.check_password("AnotherPassword123!"))
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_signup_invalid_and_empty_posts_show_errors(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "new_user",
                "password1": "AnotherPassword123!",
                "password2": "DifferentPassword123!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("password2", response.context["form"].errors)

        response = self.client.post(reverse("signup"), {})
        self.assertEqual(response.status_code, 200)
        self.assertIn("username", response.context["form"].errors)
        self.assertFalse(get_user_model().objects.filter(username="new_user").exists())

    def test_signup_duplicate_username_is_rejected(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "existing_user",
                "password1": "AnotherPassword123!",
                "password2": "AnotherPassword123!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("username", response.context["form"].errors)

    def test_authenticated_user_cannot_open_signup_or_login(self):
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get(reverse("signup")), reverse("profile"))
        self.assertRedirects(self.client.get(reverse("login")), reverse("profile"))

    def test_login_and_logout(self):
        response = self.client.post(
            reverse("login"),
            {"username": self.user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("profile"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_invalid_login_does_not_authenticate(self):
        response = self.client.post(
            reverse("login"),
            {"username": self.user.username, "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_does_not_redirect_to_external_site(self):
        response = self.client.post(
            reverse("login") + "?next=https://example.org/",
            {"username": self.user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("profile"))

    def test_profile_requires_login(self):
        response = self.client.get(reverse("profile"))
        self.assertRedirects(
            response,
            reverse("login") + "?next=" + reverse("profile"),
        )

    def test_profile_updates_only_own_contact_fields(self):
        other = get_user_model().objects.create_user(
            username="other_user", first_name="Other", password=self.password
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("profile"),
            {
                "first_name": "Alice",
                "last_name": "Example",
                "email": "alice@example.org",
                "username": "changed_username",
                "is_staff": "on",
            },
        )

        self.assertRedirects(response, reverse("profile"))
        self.user.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.user.first_name, "Alice")
        self.assertEqual(self.user.last_name, "Example")
        self.assertEqual(self.user.email, "alice@example.org")
        self.assertEqual(self.user.username, "existing_user")
        self.assertFalse(self.user.is_staff)
        self.assertEqual(other.first_name, "Other")

    def test_profile_rejects_invalid_email(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("profile"), {"email": "invalid-email"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")

    def test_password_change_requires_login(self):
        response = self.client.get(reverse("password_change"))
        self.assertRedirects(
            response,
            reverse("login") + "?next=" + reverse("password_change"),
        )

    def test_password_change_preserves_session(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": self.password,
                "new_password1": "ChangedPassword123!",
                "new_password2": "ChangedPassword123!",
            },
            follow=True,
        )
        self.assertRedirects(response, reverse("profile"))
        self.assertContains(response, "Пароль изменён.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("ChangedPassword123!"))
        self.assertFalse(self.user.check_password(self.password))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_csrf_protects_signup_profile_and_logout(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("signup"), {}).status_code, 403)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse("profile"), {}).status_code, 403)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)

    def test_forms_work_with_csrf_checks_enabled(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("signup"))
        token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("signup"),
            {
                "username": "csrf_user",
                "password1": "AnotherPassword123!",
                "password2": "AnotherPassword123!",
            },
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertRedirects(response, reverse("profile"))

        client.get(reverse("profile"))
        token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("profile"),
            {"first_name": "CSRF", "last_name": "User", "email": ""},
            HTTP_X_CSRFTOKEN=token,
            follow=True,
        )
        self.assertRedirects(response, reverse("profile"))
        self.assertContains(response, "Контактные данные сохранены.")

        token = client.cookies["csrftoken"].value
        response = client.post(reverse("logout"), HTTP_X_CSRFTOKEN=token)
        self.assertRedirects(response, reverse("login"))

    def test_navigation_matches_authentication_state(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, reverse("signup"))
        self.assertNotContains(response, 'action="' + reverse("logout") + '"')

        self.client.force_login(self.user)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, self.user.username)
        self.assertContains(response, 'action="' + reverse("logout") + '"')

    def test_root_redirects_to_competitions(self):
        self.assertRedirects(self.client.get("/"), reverse("competition_list"))
