# -*- coding: utf-8 -*-
from __future__ import absolute_import

import dateutil.parser
import hashlib
import hmac
import logging
import six

from django.db import IntegrityError, transaction
from django.http import HttpResponse, Http404
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from django.utils import timezone
from simplejson import JSONDecodeError
from sentry import options
from sentry.models import (
    Commit, CommitAuthor, CommitFileChange, Integration,
    Repository, User
)
from sentry.utils import json

from sentry.integrations.exceptions import ApiError
from .repository import GitHubRepositoryProvider
from .client import GitHubAppsClient

logger = logging.getLogger('sentry.webhooks')


def is_anonymous_email(email):
    return email[-25:] == '@users.noreply.github.com'


def get_external_id(username):
    return 'github:%s' % username


class Webhook(object):
    def __call__(self, event, organization=None):
        raise NotImplementedError


class InstallationEventWebhook(Webhook):
    # https://developer.github.com/v3/activity/events/types/#installationevent
    def __call__(self, event, organization=None):
        pass


class InstallationRepositoryEventWebhook(Webhook):
    # https://developer.github.com/v3/activity/events/types/#installationrepositoriesevent
    def __call__(self, event, organization=None):
        pass


class PushEventWebhook(Webhook):
    def _handle(self, event, organization, is_apps):
        authors = {}
        client = GitHubAppsClient(options.get('github-app.id'))
        gh_username_cache = {}

        try:
            repo = Repository.objects.get(
                organization_id=organization.id,
                provider='integrations:github',
                external_id=six.text_type(event['repository']['id']),
            )
        except Repository.DoesNotExist:
            raise Http404()

        # We need to track GitHub's "full_name" which is the repository slug.
        # This is needed to access the API since `external_id` isn't sufficient.
        if repo.config.get('name') != event['repository']['full_name']:
            repo.config['name'] = event['repository']['full_name']
            repo.save()

        for commit in event['commits']:
            if not commit['distinct']:
                continue

            if GitHubRepositoryProvider.should_ignore_commit(commit['message']):
                continue

            author_email = commit['author']['email']
            if '@' not in author_email:
                author_email = u'{}@localhost'.format(
                    author_email[:65],
                )
            # try to figure out who anonymous emails are
            elif is_anonymous_email(author_email):
                gh_username = commit['author'].get('username')
                # bot users don't have usernames
                if gh_username:
                    external_id = get_external_id(gh_username)
                    if gh_username in gh_username_cache:
                        author_email = gh_username_cache[gh_username] or author_email
                    else:
                        try:
                            commit_author = CommitAuthor.objects.get(
                                external_id=external_id,
                                organization_id=organization.id,
                            )
                        except CommitAuthor.DoesNotExist:
                            commit_author = None

                        if commit_author is not None and not is_anonymous_email(
                            commit_author.email
                        ):
                            author_email = commit_author.email
                            gh_username_cache[gh_username] = author_email
                        else:
                            try:
                                gh_user = client.get('/users/%s' % gh_username)
                            except ApiError as exc:
                                logger.exception(six.text_type(exc))
                            else:
                                # even if we can't find a user, set to none so we
                                # don't re-query
                                gh_username_cache[gh_username] = None
                                try:
                                    user = User.objects.filter(
                                        social_auth__provider='github',
                                        social_auth__uid=gh_user['id'],
                                        org_memberships=organization,
                                    )[0]
                                except IndexError:
                                    pass
                                else:
                                    author_email = user.email
                                    gh_username_cache[gh_username] = author_email
                                    if commit_author is not None:
                                        try:
                                            with transaction.atomic():
                                                commit_author.update(
                                                    email=author_email,
                                                    external_id=external_id,
                                                )
                                        except IntegrityError:
                                            pass

                        if commit_author is not None:
                            authors[author_email] = commit_author

            # TODO(dcramer): we need to deal with bad values here, but since
            # its optional, lets just throw it out for now
            if len(author_email) > 75:
                author = None
            elif author_email not in authors:
                authors[author_email] = author = CommitAuthor.objects.get_or_create(
                    organization_id=organization.id,
                    email=author_email,
                    defaults={
                        'name': commit['author']['name'][:128],
                    }
                )[0]

                update_kwargs = {}

                if author.name != commit['author']['name'][:128]:
                    update_kwargs['name'] = commit['author']['name'][:128]

                gh_username = commit['author'].get('username')
                if gh_username:
                    external_id = get_external_id(gh_username)
                    if author.external_id != external_id and not is_anonymous_email(author.email):
                        update_kwargs['external_id'] = external_id

                if update_kwargs:
                    try:
                        with transaction.atomic():
                            author.update(**update_kwargs)
                    except IntegrityError:
                        pass
            else:
                author = authors[author_email]

            try:
                with transaction.atomic():
                    c = Commit.objects.create(
                        repository_id=repo.id,
                        organization_id=organization.id,
                        key=commit['id'],
                        message=commit['message'],
                        author=author,
                        date_added=dateutil.parser.parse(
                            commit['timestamp'],
                        ).astimezone(timezone.utc),
                    )
                    for fname in commit['added']:
                        CommitFileChange.objects.create(
                            organization_id=organization.id,
                            commit=c,
                            filename=fname,
                            type='A',
                        )
                    for fname in commit['removed']:
                        CommitFileChange.objects.create(
                            organization_id=organization.id,
                            commit=c,
                            filename=fname,
                            type='D',
                        )
                    for fname in commit['modified']:
                        CommitFileChange.objects.create(
                            organization_id=organization.id,
                            commit=c,
                            filename=fname,
                            type='M',
                        )
            except IntegrityError:
                pass

    # https://developer.github.com/v3/activity/events/types/#pushevent
    def __call__(self, event, organization=None):
        is_apps = 'installation' in event
        if organization is None:
            if 'installation' not in event:
                return
            integration = Integration.objects.get(
                external_id=event['installation']['id'],
                provider='github',
            )
            organizations = list(integration.organizations.all())
        else:
            organizations = [organization]

        for org in organizations:
            self._handle(event, org, is_apps)


class GithubWebhookBase(View):
    _handlers = {
        'push': PushEventWebhook,
    }

    # https://developer.github.com/webhooks/
    def get_handler(self, event_type):
        return self._handlers.get(event_type)

    def is_valid_signature(self, method, body, secret, signature):
        if method == 'sha1':
            mod = hashlib.sha1
        else:
            raise NotImplementedError('signature method %s is not supported' % (method, ))
        expected = hmac.new(
            key=secret.encode('utf-8'),
            msg=body,
            digestmod=mod,
        ).hexdigest()
        return constant_time_compare(expected, signature)

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        if request.method != 'POST':
            return HttpResponse(status=405)

        return super(GithubWebhookBase, self).dispatch(request, *args, **kwargs)

    def get_logging_data(self, organization):
        pass

    def get_secret(self, organization):
        raise NotImplementedError

    def handle(self, request, organization=None):
        secret = self.get_secret(organization)

        if secret is None:
            logger.error(
                'github.webhook.missing-secret',
                extra=self.get_logging_data(organization),
            )
            return HttpResponse(status=401)

        body = six.binary_type(request.body)
        if not body:
            logger.error(
                'github.webhook.missing-body',
                extra=self.get_logging_data(organization),
            )
            return HttpResponse(status=400)

        try:
            handler = self.get_handler(request.META['HTTP_X_GITHUB_EVENT'])
        except KeyError:
            logger.error(
                'github.webhook.missing-event',
                extra=self.get_logging_data(organization),
            )
            return HttpResponse(status=400)

        if not handler:
            return HttpResponse(status=204)

        try:
            method, signature = request.META['HTTP_X_HUB_SIGNATURE'].split('=', 1)
        except (KeyError, IndexError):
            logger.error(
                'github.webhook.missing-signature',
                extra=self.get_logging_data(organization),
            )
            return HttpResponse(status=400)

        if not self.is_valid_signature(method, body, self.get_secret(organization), signature):
            logger.error(
                'github.webhook.invalid-signature',
                extra=self.get_logging_data(organization),
            )
            return HttpResponse(status=401)

        try:
            event = json.loads(body.decode('utf-8'))
        except JSONDecodeError:
            logger.error(
                'github.webhook.invalid-json',
                extra=self.get_logging_data(organization),
                exc_info=True,
            )
            return HttpResponse(status=400)

        handler()(event, organization=organization)
        return HttpResponse(status=204)


class GithubIntegrationsWebhookEndpoint(GithubWebhookBase):
    _handlers = {
        'push': PushEventWebhook,
        'installation': InstallationEventWebhook,
        'installation_repositories': InstallationRepositoryEventWebhook,
    }

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        if request.method != 'POST':
            return HttpResponse(status=405)

        return super(GithubIntegrationsWebhookEndpoint, self).dispatch(request, *args, **kwargs)

    def get_secret(self, organization):
        return options.get('github-app.webhook-secret')

    def post(self, request):
        return self.handle(request)
