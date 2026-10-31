from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from .models import Team, TeamInvitation, TeamMembership
from .team_services import create_team, invite_user


class TeamWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.captain = User.objects.create_user(username="captain")
        cls.member = User.objects.create_user(username="member")
        cls.outsider = User.objects.create_user(username="outsider")
        cls.team = Team.objects.create(name="Red Team")
        cls.captain_membership = TeamMembership.objects.create(
            team=cls.team, user=cls.captain, role="captain"
        )
        cls.member_membership = TeamMembership.objects.create(
            team=cls.team, user=cls.member
        )

    def test_creating_team_assigns_creator_as_captain(self):
        self.assertRedirects(
            self.client.get(reverse("team_create")),
            reverse("login") + "?next=" + reverse("team_create"),
        )
        self.client.force_login(self.outsider)
        response = self.client.post(reverse("team_create"), {"name": " Blue Team "})
        created = Team.objects.get(name="Blue Team")
        self.assertRedirects(
            response, reverse("team_detail", args=[created.pk])
        )
        self.assertTrue(
            TeamMembership.objects.filter(
                team=created, user=self.outsider, role="captain"
            ).exists()
        )

        response = self.client.post(reverse("team_create"), {"name": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Team.objects.count(), 2)

    @patch("competitions.team_services.TeamMembership.objects.create")
    def test_team_creation_rolls_back_if_captain_cannot_be_added(self, mock_create):
        mock_create.side_effect = RuntimeError("membership failure")
        with self.assertRaisesRegex(RuntimeError, "membership failure"):
            create_team("Incomplete Team", self.outsider)
        self.assertFalse(Team.objects.filter(name="Incomplete Team").exists())

    def test_team_page_requires_membership_and_hides_captain_controls(self):
        url = reverse("team_detail", args=[self.team.pk])
        self.assertRedirects(self.client.get(url), reverse("login") + "?next=" + url)

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.member)
        response = self.client.get(url)
        self.assertContains(response, "Red Team")
        self.assertNotContains(response, reverse("team_invite", args=[self.team.pk]))

        self.client.force_login(self.captain)
        self.assertContains(
            self.client.get(url), reverse("team_invite", args=[self.team.pk])
        )

    def test_captain_can_invite_registered_user_once(self):
        self.client.force_login(self.captain)
        url = reverse("team_invite", args=[self.team.pk])
        response = self.client.post(url, {"username": self.outsider.username})
        self.assertRedirects(response, reverse("team_detail", args=[self.team.pk]))
        invitation = TeamInvitation.objects.get(team=self.team, invitee=self.outsider)
        self.assertEqual(invitation.invited_by_id, self.captain.pk)
        self.assertEqual(invitation.status, "pending")

        duplicate = self.client.post(url, {"username": self.outsider.username})
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.context["invitation_form"].non_field_errors())
        self.assertEqual(TeamInvitation.objects.count(), 1)

        unknown = self.client.post(url, {"username": "missing_user"})
        self.assertEqual(unknown.status_code, 200)
        self.assertTrue(unknown.context["invitation_form"].non_field_errors())

        existing = self.client.post(url, {"username": self.member.username})
        self.assertEqual(existing.status_code, 200)
        self.assertTrue(existing.context["invitation_form"].non_field_errors())

    def test_non_captains_cannot_invite_or_cancel(self):
        invitation = invite_user(self.team, self.captain, self.outsider.username)
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.post(
                reverse("team_invite", args=[self.team.pk]),
                {"username": "another_user"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("team_invitation_cancel", args=[invitation.pk])
            ).status_code,
            403,
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "pending")

    def test_only_invitee_can_accept_and_replay_is_rejected(self):
        invitation = invite_user(self.team, self.captain, self.outsider.username)
        url = reverse("team_invitation_accept", args=[invitation.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url).status_code, 403)

        self.client.force_login(self.outsider)
        self.assertRedirects(
            self.client.post(url), reverse("team_detail", args=[self.team.pk])
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "accepted")
        self.assertTrue(
            TeamMembership.objects.filter(
                team=self.team, user=self.outsider, role="member"
            ).exists()
        )
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(
            TeamMembership.objects.filter(team=self.team, user=self.outsider).count(),
            1,
        )

    def test_declined_invitation_can_be_reissued_and_revoked(self):
        invitation = invite_user(self.team, self.captain, self.outsider.username)
        self.client.force_login(self.outsider)
        self.assertRedirects(
            self.client.post(reverse("team_invitation_decline", args=[invitation.pk])),
            reverse("profile"),
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "declined")
        self.assertFalse(
            TeamMembership.objects.filter(team=self.team, user=self.outsider).exists()
        )

        second = invite_user(self.team, self.captain, self.outsider.username)
        self.client.force_login(self.captain)
        self.assertRedirects(
            self.client.post(reverse("team_invitation_cancel", args=[second.pk])),
            reverse("team_detail", args=[self.team.pk]),
        )
        second.refresh_from_db()
        self.assertEqual(second.status, "cancelled")
        self.client.force_login(self.outsider)
        self.assertEqual(
            self.client.post(
                reverse("team_invitation_accept", args=[second.pk])
            ).status_code,
            400,
        )

    def test_pending_invitations_are_unique_in_database(self):
        TeamInvitation.objects.create(
            team=self.team, invitee=self.outsider, invited_by=self.captain
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamInvitation.objects.create(
                    team=self.team, invitee=self.outsider, invited_by=self.captain
                )

    def test_transfer_changes_captain_and_permissions(self):
        self.client.force_login(self.captain)
        response = self.client.post(
            reverse(
                "team_transfer_captain",
                args=[self.team.pk, self.member_membership.pk],
            )
        )
        self.assertRedirects(response, reverse("team_detail", args=[self.team.pk]))
        self.captain_membership.refresh_from_db()
        self.member_membership.refresh_from_db()
        self.assertEqual(self.captain_membership.role, "member")
        self.assertEqual(self.member_membership.role, "captain")
        self.assertEqual(
            self.client.post(
                reverse("team_invite", args=[self.team.pk]),
                {"username": self.outsider.username},
            ).status_code,
            403,
        )
        self.client.force_login(self.member)
        self.assertRedirects(
            self.client.post(
                reverse("team_invite", args=[self.team.pk]),
                {"username": self.outsider.username},
            ),
            reverse("team_detail", args=[self.team.pk]),
        )

    def test_only_captain_can_transfer_or_remove_member(self):
        transfer = reverse(
            "team_transfer_captain", args=[self.team.pk, self.captain_membership.pk]
        )
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(transfer).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse(
                    "team_remove_member",
                    args=[self.team.pk, self.captain_membership.pk],
                )
            ).status_code,
            403,
        )

        self.client.force_login(self.captain)
        self.assertEqual(self.client.post(transfer).status_code, 400)
        self.assertEqual(
            self.client.post(
                reverse(
                    "team_remove_member",
                    args=[self.team.pk, self.captain_membership.pk],
                )
            ).status_code,
            400,
        )
        self.captain_membership.refresh_from_db()
        self.assertEqual(self.captain_membership.role, "captain")

    def test_captain_cannot_act_on_membership_from_another_team(self):
        another_team = Team.objects.create(name="Blue Team")
        other_membership = TeamMembership.objects.create(
            team=another_team, user=self.outsider
        )
        self.client.force_login(self.captain)
        self.assertEqual(
            self.client.post(
                reverse(
                    "team_transfer_captain",
                    args=[self.team.pk, other_membership.pk],
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "team_remove_member",
                    args=[self.team.pk, other_membership.pk],
                )
            ).status_code,
            404,
        )
        self.assertTrue(
            TeamMembership.objects.filter(pk=other_membership.pk).exists()
        )

    def test_captain_can_remove_member_and_member_can_leave(self):
        self.client.force_login(self.captain)
        self.assertRedirects(
            self.client.post(
                reverse(
                    "team_remove_member",
                    args=[self.team.pk, self.member_membership.pk],
                )
            ),
            reverse("team_detail", args=[self.team.pk]),
        )
        self.assertFalse(
            TeamMembership.objects.filter(pk=self.member_membership.pk).exists()
        )

        TeamMembership.objects.create(team=self.team, user=self.member)
        self.client.force_login(self.member)
        self.assertRedirects(
            self.client.post(reverse("team_leave", args=[self.team.pk])),
            reverse("profile"),
        )
        self.assertEqual(
            self.client.get(reverse("team_detail", args=[self.team.pk])).status_code,
            403,
        )

    def test_captain_must_transfer_before_leaving(self):
        self.client.force_login(self.captain)
        self.assertEqual(
            self.client.post(reverse("team_leave", args=[self.team.pk])).status_code,
            400,
        )
        self.assertTrue(
            TeamMembership.objects.filter(pk=self.captain_membership.pk).exists()
        )

    def test_profile_shows_own_teams_and_pending_invitations(self):
        invitation = invite_user(self.team, self.captain, self.outsider.username)
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, "Red Team")
        self.assertContains(
            response, reverse("team_invitation_accept", args=[invitation.pk])
        )

        self.client.post(reverse("team_invitation_accept", args=[invitation.pk]))
        response = self.client.get(reverse("profile"))
        self.assertContains(response, reverse("team_detail", args=[self.team.pk]))
        self.assertNotContains(
            response, reverse("team_invitation_accept", args=[invitation.pk])
        )

    def test_mutations_require_post_and_csrf(self):
        self.client.force_login(self.captain)
        self.assertEqual(
            self.client.get(reverse("team_invite", args=[self.team.pk])).status_code,
            405,
        )
        self.assertEqual(
            self.client.get(reverse("team_leave", args=[self.team.pk])).status_code,
            405,
        )

        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.captain)
        url = reverse("team_invite", args=[self.team.pk])
        self.assertEqual(
            strict_client.post(url, {"username": self.outsider.username}).status_code,
            403,
        )
        strict_client.get(reverse("team_detail", args=[self.team.pk]))
        token = strict_client.cookies["csrftoken"].value
        self.assertRedirects(
            strict_client.post(
                url,
                {"username": self.outsider.username},
                HTTP_X_CSRFTOKEN=token,
            ),
            reverse("team_detail", args=[self.team.pk]),
        )
