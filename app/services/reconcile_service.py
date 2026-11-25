"""
Reconcile runner VMs with GitHub state.

The webhook path creates one VM per `workflow_job.queued` event and deletes it on
`completed`. Two cases leave GitHub and GCE out of step:

* Creating the VM fails, for example with QUOTA_EXCEEDED when several pushes land
  at once. GitHub does not resend the webhook, so the job stays queued forever.
* A job is cancelled before any runner picked it up. The `completed` event has no
  runner name, so its VM is never deleted and idles until max_run_duration.

`reconcile()` fixes both and is safe to run repeatedly (Cloud Scheduler every few
minutes): it only creates a VM for a queued job when fewer runner VMs are already
starting than there are queued jobs, and only deletes VMs that are old enough and
not registered with GitHub.
"""
import logging
import os
from datetime import datetime, timezone

from app.clients import GitHubClient, GCloudClient
from app.services.webhook_service import parse_allowed_repositories

logger = logging.getLogger(__name__)

# A VM needs about a minute to boot and register. Before that it is "starting", not orphaned
DEFAULT_ORPHAN_GRACE_SECONDS = 10 * 60
# Booting VMs that have not registered yet are counted as covering one queued job each
STARTING_STATUSES = {'PROVISIONING', 'STAGING', 'RUNNING'}


def _age_seconds(created, now):
    try:
        started = datetime.fromisoformat(created.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    return (now - started).total_seconds()


def _runner_label(labels):
    for label in labels:
        if label.startswith('gcp-'):
            return label
    return None


class ReconcileService:
    """Create runners for stranded queued jobs and delete orphaned runner VMs."""

    def __init__(self):
        self.github_client = GitHubClient()
        self.gcloud_client = GCloudClient()
        self.allowed_repositories = sorted(parse_allowed_repositories(os.environ.get('ALLOWED_REPOSITORIES')))
        self.orphan_grace_seconds = int(
            os.environ.get('ORPHAN_GRACE_SECONDS', DEFAULT_ORPHAN_GRACE_SECONDS)
        )

    def reconcile(self, now=None):
        """Run one reconcile pass. Returns a summary dict for logging and the HTTP response."""
        now = now or datetime.now(timezone.utc)
        summary = {'repositories': [], 'created': [], 'deleted': [], 'errors': []}
        if not self.allowed_repositories:
            # Without an allowlist we do not know which repositories to poll
            summary['errors'].append('ALLOWED_REPOSITORIES is empty; nothing to reconcile')
            return summary

        try:
            instances = self.gcloud_client.list_runner_instances()
        except Exception as error:  # noqa: BLE001 - report, never crash the endpoint
            summary['errors'].append(f'list instances: {error}')
            return summary

        for repo_name in self.allowed_repositories:
            summary['repositories'].append(repo_name)
            try:
                registered = self.github_client.list_runner_names(repo_name)
                queued = self.github_client.list_queued_jobs(repo_name)
            except Exception as error:  # noqa: BLE001
                summary['errors'].append(f'{repo_name}: {error}')
                continue
            repo_short = repo_name.split('/')[-1].lower()
            repo_instances = [item for item in instances if item.get('repo') in (None, repo_short)]
            self._delete_orphans(repo_instances, registered, now, summary)
            self._create_for_queued(repo_name, repo_instances, registered, queued, summary)
        logger.info('Reconcile finished: %s', summary)
        return summary

    def _delete_orphans(self, instances, registered, now, summary):
        for instance in instances:
            name = instance['name']
            if name in registered:
                continue
            age = _age_seconds(instance.get('created'), now)
            if age is None or age < self.orphan_grace_seconds:
                continue
            try:
                self.gcloud_client.delete_runner_instance(name, delivery_id='reconcile')
                summary['deleted'].append(name)
            except Exception as error:  # noqa: BLE001
                summary['errors'].append(f'delete {name}: {error}')

    def _create_for_queued(self, repo_name, instances, registered, queued, summary):
        jobs = [job for job in queued if _runner_label(job.get('labels', []))]
        # VMs that exist but have not registered yet will pick up queued jobs soon
        starting = sum(
            1 for item in instances
            if item['name'] not in registered
            and item['name'] not in summary['deleted']
            and item.get('status') in STARTING_STATUSES
        )
        missing = len(jobs) - starting
        if missing <= 0:
            return
        repo_url = f'https://github.com/{repo_name}'
        for job in jobs[:missing]:
            template = _runner_label(job['labels'])
            try:
                token = self.github_client.get_registration_token(repo_name=repo_name, delivery_id='reconcile')
                name = self.gcloud_client.create_runner_instance(
                    token, repo_url, template, repo_name, delivery_id='reconcile'
                )
                if name:
                    summary['created'].append(name)
            except Exception as error:  # noqa: BLE001 - quota may still be exhausted; try next pass
                summary['errors'].append(f'create for job {job.get("id")}: {error}')
                break
