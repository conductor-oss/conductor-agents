"""Route-extraction patterns and tool resolution."""
import sys


def _match_routes(text):
    from sast.tasks import ROUTE_PATTERNS
    found = set()
    for rx, m_grp, p_grp in ROUTE_PATTERNS:
        for m in rx.finditer(text):
            path = m.group(p_grp)
            method = (m.group(m_grp).upper() if m_grp else "GET")
            if path and path.startswith("/"):
                found.add((method, path))
    return found


def test_express_routes():
    code = "app.get('/search', h); router.post(\"/login\", h); api.delete('/users/:id', h)"
    r = _match_routes(code)
    assert ("GET", "/search") in r
    assert ("POST", "/login") in r
    assert ("DELETE", "/users/:id") in r


def test_flask_fastapi_routes():
    code = "@app.route('/admin')\n@router.get('/items')\n@app.post('/submit')"
    r = _match_routes(code)
    assert ("GET", "/admin") in r       # @app.route defaults to GET
    assert ("GET", "/items") in r
    assert ("POST", "/submit") in r


def test_spring_routes():
    code = '@GetMapping("/api/v1/users")\n@PostMapping(value="/api/v1/orders")'
    r = _match_routes(code)
    assert ("GET", "/api/v1/users") in r
    assert ("POST", "/api/v1/orders") in r


def test_tool_resolution_finds_venv_python():
    from sast.tasks import _tool
    # the running interpreter's own bin dir always has 'python'
    import os
    name = "python" if os.path.exists(os.path.join(os.path.dirname(sys.executable), "python")) else "python3"
    assert _tool(name) is not None
