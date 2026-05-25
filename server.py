from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import requests as req

app = Flask(__name__, static_folder='.')
CORS(app)

# Dashboard login password
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

# Each app has its own auth style and route structure
APPS = {
    'tp': {
        'name': 'TruthPrism',
        'base_url': os.environ.get('TP_URL', 'https://app.truthprism.app'),
        # Auth: JSON body {"admin_secret": "..."}
        'auth_style': 'json_secret',
        'admin_pw': os.environ.get('TP_ADMIN_PW', ''),
        'auth_key': 'admin_secret',
        # Routes
        'list_method': 'POST',
        'list_path': '/api/admin/list-codes',
        'list_codes_key': 'codes',       # response is {"codes": [...]}
        'toggle_path': None,             # no toggle; has deactivate only
        'deactivate_path': '/api/admin/deactivate-code',
        'delete_path': '/api/admin/delete-code',
        'create_path': '/api/admin/create-code',
    },
    'nd': {
        'name': 'News-Distiller',
        'base_url': os.environ.get('ND_URL', 'https://app.news-distiller.com'),
        # Auth: header X-Admin-Password
        'auth_style': 'header',
        'admin_pw': os.environ.get('ND_ADMIN_PW', ''),
        # Routes
        'list_method': 'GET',
        'list_path': '/api/admin/codes',
        'list_codes_key': None,          # response is directly a list
        'toggle_path': '/api/admin/codes/{code}/toggle',
        'delete_path': '/api/admin/codes/{code}/delete',
        'create_path': '/api/admin/codes',
    },
    'sp': {
        'name': 'ScamPrism',
        'base_url': os.environ.get('SP_URL', 'https://app.scamprism.com'),
        # Auth: JSON body {"admin_code": "..."}
        'auth_style': 'json_secret',
        'admin_pw': os.environ.get('SP_ADMIN_PW', ''),
        'auth_key': 'admin_code',
        # Routes
        'list_method': 'POST',
        'list_path': '/api/admin/list-codes',
        'list_codes_key': 'codes',       # response is {"codes": [...], "stats": {...}}
        'toggle_path': None,
        'revoke_path': '/api/admin/revoke-code',
        'unrevoke_path': '/api/admin/unrevoke-code',
        'delete_path': '/api/admin/delete-code',
        'create_path': '/api/admin/create-code',
    },
}

def check_auth():
    return request.headers.get('X-Admin-Password', '') == ADMIN_PASSWORD

def app_headers(app_id):
    """Return auth headers/body prefix for each app's style."""
    cfg = APPS[app_id]
    if cfg['auth_style'] == 'header':
        return {'X-Admin-Password': cfg['admin_pw']}, {}
    else:
        return {}, {cfg['auth_key']: cfg['admin_pw']}

def app_request(method, app_id, path, extra_body=None):
    cfg = APPS[app_id]
    url = cfg['base_url'] + path
    headers, body = app_headers(app_id)
    headers['Content-Type'] = 'application/json'
    if extra_body:
        body.update(extra_body)
    try:
        if method == 'GET':
            r = req.get(url, headers=headers, timeout=10)
        else:
            r = req.post(url, headers=headers, json=body if body else None, timeout=10)
        return r.json(), r.status_code
    except Exception as e:
        return {'error': str(e)}, 502

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/apps', methods=['GET'])
def list_apps():
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({k: {'name': v['name'], 'base_url': v['base_url']} for k, v in APPS.items()})

@app.route('/api/proxy/codes/<app_id>', methods=['GET'])
def proxy_get_codes(app_id):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    if app_id not in APPS:
        return jsonify({'error': 'Unknown app'}), 404

    cfg = APPS[app_id]
    data, status = app_request(cfg['list_method'], app_id, cfg['list_path'])

    if status != 200:
        return jsonify(data), status

    # Normalize: always return a flat list of codes
    codes_key = cfg.get('list_codes_key')
    codes = data[codes_key] if codes_key else data

    # Normalize field names across apps
    normalized = []
    for c in codes:
        normalized.append({
            'code': c.get('code', ''),
            'label': c.get('label', ''),
            'active': not c.get('revoked', False) if 'revoked' in c else bool(c.get('active', True)),
            'use_count': c.get('uses_consumed', c.get('use_count', 0)),
            'max_uses': c.get('uses_remaining', None) if 'uses_remaining' in c and 'uses_consumed' in c
                        else c.get('max_uses', None),
            'platform': c.get('platform', None),
            'last_used': c.get('last_used', None),
            'created_at': c.get('created_at', ''),
            '_raw': c,
        })

    # Also pass through stats if present (ScamPrism)
    result = {'codes': normalized}
    if isinstance(data, dict) and 'stats' in data:
        result['stats'] = data['stats']
    return jsonify(result)

@app.route('/api/proxy/codes/<app_id>/<path:code>/toggle', methods=['POST'])
def proxy_toggle(app_id, code):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    if app_id not in APPS:
        return jsonify({'error': 'Unknown app'}), 404
    cfg = APPS[app_id]

    # Determine current state first, then toggle
    list_data, _ = app_request(cfg['list_method'], app_id, cfg['list_path'])
    codes_key = cfg.get('list_codes_key')
    codes = list_data[codes_key] if codes_key and isinstance(list_data, dict) else list_data
    current = next((c for c in codes if c.get('code') == code), None)

    if cfg.get('toggle_path'):
        path = cfg['toggle_path'].format(code=code)
        data, status = app_request('POST', app_id, path)
    elif 'revoked' in (current or {}):
        # ScamPrism: use revoke/unrevoke
        is_revoked = current.get('revoked', False)
        path = cfg['unrevoke_path'] if is_revoked else cfg['revoke_path']
        data, status = app_request('POST', app_id, path, {'code': code})
    else:
        # TruthPrism: deactivate only (no re-activate endpoint)
        is_active = current.get('active', True) if current else True
        if is_active:
            data, status = app_request('POST', app_id, cfg['deactivate_path'], {'code': code})
        else:
            return jsonify({'error': 'TruthPrism does not support re-activating codes'}), 400

    return jsonify(data), status

@app.route('/api/proxy/codes/<app_id>/<path:code>/delete', methods=['POST'])
def proxy_delete(app_id, code):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    if app_id not in APPS:
        return jsonify({'error': 'Unknown app'}), 404
    cfg = APPS[app_id]
    path = cfg['delete_path'].format(code=code) if '{code}' in cfg['delete_path'] else cfg['delete_path']
    data, status = app_request('POST', app_id, path, {'code': code})
    return jsonify(data), status

@app.route('/api/proxy/codes/<app_id>', methods=['POST'])
def proxy_create(app_id):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    if app_id not in APPS:
        return jsonify({'error': 'Unknown app'}), 404
    cfg = APPS[app_id]
    body = request.get_json() or {}

    # ND uses GET-style list but POST for create with header auth
    if cfg['auth_style'] == 'header':
        url = cfg['base_url'] + cfg['create_path']
        try:
            r = req.post(url, headers={'X-Admin-Password': cfg['admin_pw'], 'Content-Type': 'application/json'}, json=body, timeout=10)
            return jsonify(r.json()), r.status_code
        except Exception as e:
            return jsonify({'error': str(e)}), 502

    data, status = app_request('POST', app_id, cfg['create_path'], body)
    return jsonify(data), status

@app.route('/api/proxy/codes/<app_id>/<path:code>/update', methods=['POST'])
def proxy_update(app_id, code):
    if not check_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    if app_id not in APPS:
        return jsonify({'error': 'Unknown app'}), 404
    cfg = APPS[app_id]
    if 'update_path' not in cfg:
        return jsonify({'error': 'Update not supported for this app'}), 400
    body = request.get_json() or {}
    path = cfg['update_path'].format(code=code)
    data, status = app_request('POST', app_id, path, {'code': code, 'max_uses': body.get('max_uses')})
    return jsonify(data), status

@app.route('/health')
def health():
    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
