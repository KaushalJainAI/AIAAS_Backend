"""
Mission deletion: cancel-first, owner-only, history survives.

Past runs keep their rows — their `mission` FK is SET_NULL — so deleting the
goal never deletes the evidence. Foreign ids are 404, never 403.
"""
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from agents.models import SubAgent
from logs.models import ExecutionLog
from missions.models import Mission


class MissionDeleteTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='Scout')

    def _mission(self, **fields):
        defaults = dict(user=self.user, agent=self.agent, goal='Ship it')
        defaults.update(fields)
        return Mission.objects.create(**defaults)

    def _url(self, mission):
        return f'/api/missions/{mission.id}/'

    def test_active_mission_must_be_cancelled_first(self):
        mission = self._mission(status='active')
        response = self.client.delete(self._url(mission))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Mission.objects.filter(id=mission.id).exists())

    def test_cancelled_mission_deletes_and_keeps_runs(self):
        mission = self._mission(status='cancelled')
        log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='completed',
            mission=mission,
        )
        response = self.client.delete(self._url(mission))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Mission.objects.filter(id=mission.id).exists())
        log.refresh_from_db()
        self.assertIsNone(log.mission_id)

    def test_foreign_mission_is_404(self):
        mission = self._mission(status='done')
        self.client.force_authenticate(user=self.other)
        response = self.client.delete(self._url(mission))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Mission.objects.filter(id=mission.id).exists())
