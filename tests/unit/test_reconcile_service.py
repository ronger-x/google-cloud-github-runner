from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from app.services.reconcile_service import ReconcileService

NOW = datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc)


def _created(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _vm(name, minutes_ago, status='RUNNING', repo='rodak'):
    return {'name': name, 'status': status, 'created': _created(minutes_ago), 'repo': repo}


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv('ALLOWED_REPOSITORIES', 'rymcu/rodak')
    with patch('app.services.reconcile_service.GitHubClient') as gh_class, \
            patch('app.services.reconcile_service.GCloudClient') as gc_class:
        gh = Mock()
        gh.list_runner_names.return_value = set()
        gh.list_queued_jobs.return_value = []
        gh.get_registration_token.return_value = 'reg-token'
        gc = Mock()
        gc.list_runner_instances.return_value = []
        gc.create_runner_instance.side_effect = lambda *args, **kwargs: f'gcp-runner-new{gc.create_runner_instance.call_count}'
        gh_class.return_value = gh
        gc_class.return_value = gc
        yield gh, gc


def test_queued_job_without_any_vm_gets_a_runner(clients):
    """Quota rejected the webhook's VM: nothing is starting, so reconcile creates one."""
    gh, gc = clients
    gh.list_queued_jobs.return_value = [{'id': 1, 'labels': ['gcp-rodak-linux']}]

    summary = ReconcileService().reconcile(now=NOW)

    gh.get_registration_token.assert_called_once_with(repo_name='rymcu/rodak', delivery_id='reconcile')
    gc.create_runner_instance.assert_called_once_with(
        'reg-token', 'https://github.com/rymcu/rodak', 'gcp-rodak-linux', 'rymcu/rodak', delivery_id='reconcile'
    )
    assert summary['created'] == ['gcp-runner-new1']


def test_queued_job_covered_by_a_starting_vm_is_left_alone(clients):
    """A VM created by the webhook is still booting: do not create a second one."""
    gh, gc = clients
    gh.list_queued_jobs.return_value = [{'id': 1, 'labels': ['gcp-rodak-linux']}]
    gc.list_runner_instances.return_value = [_vm('gcp-runner-booting', minutes_ago=1)]

    summary = ReconcileService().reconcile(now=NOW)

    gc.create_runner_instance.assert_not_called()
    assert summary['created'] == []


def test_only_the_missing_runners_are_created(clients):
    gh, gc = clients
    gh.list_queued_jobs.return_value = [
        {'id': 1, 'labels': ['gcp-rodak-linux']},
        {'id': 2, 'labels': ['gcp-rodak-linux']},
        {'id': 3, 'labels': ['gcp-rodak-linux']},
    ]
    gc.list_runner_instances.return_value = [_vm('gcp-runner-booting', minutes_ago=1)]

    summary = ReconcileService().reconcile(now=NOW)

    assert gc.create_runner_instance.call_count == 2
    assert len(summary['created']) == 2


def test_jobs_without_a_gcp_label_are_ignored(clients):
    gh, gc = clients
    gh.list_queued_jobs.return_value = [{'id': 1, 'labels': ['self-hosted', 'macOS', 'rodak-arm64']}]

    ReconcileService().reconcile(now=NOW)

    gc.create_runner_instance.assert_not_called()


def test_quota_error_stops_this_pass_and_is_reported(clients):
    gh, gc = clients
    gh.list_queued_jobs.return_value = [
        {'id': 1, 'labels': ['gcp-rodak-linux']},
        {'id': 2, 'labels': ['gcp-rodak-linux']},
    ]
    gc.create_runner_instance.side_effect = RuntimeError('QUOTA_EXCEEDED')

    summary = ReconcileService().reconcile(now=NOW)

    assert gc.create_runner_instance.call_count == 1
    assert summary['created'] == []
    assert any('QUOTA_EXCEEDED' in error for error in summary['errors'])


def test_unregistered_vm_older_than_grace_is_deleted(clients):
    """Job cancelled before pickup: completed event had no runner name, VM idles."""
    gh, gc = clients
    gc.list_runner_instances.return_value = [_vm('gcp-runner-orphan', minutes_ago=30)]

    summary = ReconcileService().reconcile(now=NOW)

    gc.delete_runner_instance.assert_called_once_with('gcp-runner-orphan', delivery_id='reconcile')
    assert summary['deleted'] == ['gcp-runner-orphan']


def test_young_or_registered_vms_are_kept(clients):
    gh, gc = clients
    gh.list_runner_names.return_value = {'gcp-runner-busy'}
    gc.list_runner_instances.return_value = [
        _vm('gcp-runner-booting', minutes_ago=2),
        _vm('gcp-runner-busy', minutes_ago=40),
    ]

    summary = ReconcileService().reconcile(now=NOW)

    gc.delete_runner_instance.assert_not_called()
    assert summary['deleted'] == []


def test_orphan_deleted_in_same_pass_does_not_count_as_starting(clients):
    """An old orphan must not hide a stranded job: delete it and create a fresh runner."""
    gh, gc = clients
    gh.list_queued_jobs.return_value = [{'id': 1, 'labels': ['gcp-rodak-linux']}]
    gc.list_runner_instances.return_value = [_vm('gcp-runner-orphan', minutes_ago=30)]

    summary = ReconcileService().reconcile(now=NOW)

    assert summary['deleted'] == ['gcp-runner-orphan']
    assert summary['created'] == ['gcp-runner-new1']


def test_vms_of_other_repositories_are_not_touched(clients):
    gh, gc = clients
    gc.list_runner_instances.return_value = [_vm('gcp-runner-other', minutes_ago=30, repo='other-repo')]

    ReconcileService().reconcile(now=NOW)

    gc.delete_runner_instance.assert_not_called()


def test_without_allowlist_nothing_is_done(clients, monkeypatch):
    gh, gc = clients
    monkeypatch.delenv('ALLOWED_REPOSITORIES')

    summary = ReconcileService().reconcile(now=NOW)

    gc.list_runner_instances.assert_not_called()
    assert summary['errors']


def test_github_error_for_a_repo_is_reported_not_raised(clients):
    gh, gc = clients
    gh.list_queued_jobs.side_effect = RuntimeError('HTTP 502')

    summary = ReconcileService().reconcile(now=NOW)

    assert any('HTTP 502' in error for error in summary['errors'])
    gc.create_runner_instance.assert_not_called()


def test_vm_already_shutting_down_is_not_deleted_again(clients):
    """The job just finished and the webhook is deleting the VM: do not delete it a second time."""
    gh, gc = clients
    gc.list_runner_instances.return_value = [_vm('gcp-runner-done', minutes_ago=26, status='STOPPING')]

    summary = ReconcileService().reconcile(now=NOW)

    gc.delete_runner_instance.assert_not_called()
    assert summary['deleted'] == []
