import urllib.parse

import pytest

from mf_phase12.drive_move_execute import GoogleDriveApiMoveClient
from test_transport_isolation import DriveTransport, synthetic_profile


@pytest.mark.parametrize('fault', ['duplicate_auth', 'duplicate_parent', 'body', 'content', 'dot_path'])
def test_direct_transport_rejects_ambiguous_or_expanded_operation(tmp_path, fault):
    profile = synthetic_profile(tmp_path, 'alpha')
    transport = DriveTransport(profile)
    client = GoogleDriveApiMoveClient('synthetic', office_profile=profile,
        transport=transport, write_enabled=True)
    headers = {'Authorization': 'Bearer synthetic'}
    query = [('addParents', profile.google_drive_processed_folder_id),
        ('removeParents', profile.google_drive_evidence_root_id),
        ('fields', 'id,name,parents'), ('supportsAllDrives', 'true')]
    method, path, body = 'PATCH', '/files/synthetic-file', None
    if fault == 'duplicate_auth':
        headers['authorization'] = 'Bearer foreign'
    elif fault == 'duplicate_parent':
        query.append(('addParents', 'foreign_processed'))
    elif fault == 'body':
        body = b'{"name":"unauthorized-rename"}'
    elif fault == 'content':
        method, query = 'GET', [('alt', 'media')]
    else:
        path = '/files/%2e%2e'
    with pytest.raises(PermissionError):
        client.transport.request(method, client.base_url + path + '?' + urllib.parse.urlencode(query),
            headers=headers, body=body)
    assert transport.calls == []
