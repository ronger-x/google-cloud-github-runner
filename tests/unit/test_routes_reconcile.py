from unittest.mock import patch


def test_reconcile_disabled_without_configured_token(client, monkeypatch):
    monkeypatch.delenv('RECONCILE_TOKEN', raising=False)
    with patch('app.routes.reconcile.ReconcileService') as service:
        response = client.post('/reconcile', headers={'X-Reconcile-Token': ''})
    assert response.status_code == 403
    service.assert_not_called()


def test_reconcile_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setenv('RECONCILE_TOKEN', 'right-token')
    with patch('app.routes.reconcile.ReconcileService') as service:
        response = client.post('/reconcile', headers={'X-Reconcile-Token': 'wrong'})
    assert response.status_code == 403
    service.assert_not_called()


def test_reconcile_runs_with_right_token(client, monkeypatch):
    monkeypatch.setenv('RECONCILE_TOKEN', 'right-token')
    with patch('app.routes.reconcile.ReconcileService') as service:
        service.return_value.reconcile.return_value = {
            'repositories': ['rymcu/rodak'], 'created': ['gcp-runner-x'], 'deleted': [], 'errors': []
        }
        response = client.post('/reconcile', headers={'X-Reconcile-Token': 'right-token'})
    assert response.status_code == 200
    assert response.get_json()['created'] == ['gcp-runner-x']


def test_reconcile_only_accepts_post(client):
    assert client.get('/reconcile').status_code == 405
