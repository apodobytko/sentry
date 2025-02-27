from unittest.mock import patch

import responses

from sentry import audit_log
from sentry.constants import SentryAppInstallationStatus
from sentry.models import ApiGrant, AuditLogEntry, ServiceHook, ServiceHookProject, actor
from sentry.sentry_apps import SentryAppInstallationCreator
from sentry.testutils import TestCase
from sentry.testutils.silo import control_silo_test, exempt_from_silo_limits


@control_silo_test(stable=True)
class TestCreator(TestCase):
    def setUp(self):
        actor.pre_save.disconnect(
            dispatch_uid="handle_actor_pre_save",
            sender="sentry.User",
            receiver=actor.handle_actor_pre_save,
        )
        self.user = self.create_user()
        self.org = self.create_organization()

        self.project1 = self.create_project(organization=self.org)
        self.project2 = self.create_project(organization=self.org)

        responses.add(responses.POST, "https://example.com/webhook")

        self.sentry_app = self.create_sentry_app(
            name="nulldb",
            organization_id=self.org.id,
            scopes=("project:read",),
            events=("issue.created",),
        )

    def tearDown(self):
        actor.pre_save.connect(
            actor.handle_actor_pre_save, dispatch_uid="handle_actor_pre_save", sender="sentry.User"
        )

    def run_creator(self, **kwargs):
        return SentryAppInstallationCreator(
            organization_id=self.org.id, slug="nulldb", **kwargs
        ).run(user=self.user, request=kwargs.pop("request", None))

    @responses.activate
    def test_creates_installation(self):
        responses.add(responses.POST, "https://example.com/webhook")
        install = self.run_creator()
        assert install.pk

    @responses.activate
    def test_creates_api_grant(self):
        responses.add(responses.POST, "https://example.com/webhook")
        install = self.run_creator()
        assert ApiGrant.objects.filter(id=install.api_grant_id).exists()

    @responses.activate
    def test_creates_service_hooks(self):
        responses.add(responses.POST, "https://example.com/webhook")
        install = self.run_creator()

        with exempt_from_silo_limits():
            hook = ServiceHook.objects.get(organization_id=self.org.id)

        assert hook.application_id == self.sentry_app.application.id
        assert hook.actor_id == install.id
        assert hook.organization_id == self.org.id
        assert hook.events == self.sentry_app.events
        assert hook.url == self.sentry_app.webhook_url

        with exempt_from_silo_limits():
            assert not ServiceHookProject.objects.all()

    @responses.activate
    def test_creates_audit_log_entry(self):
        responses.add(responses.POST, "https://example.com/webhook")
        request = self.make_request(user=self.user, method="GET")
        SentryAppInstallationCreator(organization_id=self.org.id, slug="nulldb").run(
            user=self.user, request=request
        )
        assert AuditLogEntry.objects.filter(
            event=audit_log.get_event_id("SENTRY_APP_INSTALL")
        ).exists()

    @responses.activate
    @patch("sentry.mediators.sentry_app_installations.InstallationNotifier.run")
    def test_notifies_service(self, run):
        with self.tasks():
            responses.add(responses.POST, "https://example.com/webhook")
            install = self.run_creator()
            run.assert_called_once_with(install=install, user=self.user, action="created")

    @responses.activate
    def test_associations(self):
        responses.add(responses.POST, "https://example.com/webhook")
        install = self.run_creator()

        assert install.api_grant is not None

    @responses.activate
    def test_pending_status(self):
        responses.add(responses.POST, "https://example.com/webhook")
        install = self.run_creator()

        assert install.status == SentryAppInstallationStatus.PENDING

    @responses.activate
    def test_installed_status(self):
        responses.add(responses.POST, "https://example.com/webhook")
        internal_app = self.create_internal_integration(name="internal", organization=self.org)
        install = SentryAppInstallationCreator(
            organization_id=self.org.id, slug=internal_app.slug
        ).run(user=self.user, request=None)

        assert install.status == SentryAppInstallationStatus.INSTALLED

    @patch("sentry.analytics.record")
    def test_records_analytics(self, record):
        SentryAppInstallationCreator(organization_id=self.org.id, slug="nulldb",).run(
            user=self.user,
            request=self.make_request(user=self.user, method="GET"),
        )

        record.assert_called_with(
            "sentry_app.installed",
            user_id=self.user.id,
            organization_id=self.org.id,
            sentry_app="nulldb",
        )
