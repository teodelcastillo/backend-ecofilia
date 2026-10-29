"""
Recorre el contrato de aislamiento por HTTP, como lo haría el frontend.

Requiere un backend LOCAL con la base sembrada (``manage.py seed_local_instituciones``):

    python scripts/verificar_contrato_local.py                       # http://localhost:8000/api
    python scripts/verificar_contrato_local.py http://localhost:8000/api

Se niega a apuntar a producción. Imprime, por usuario, lo que ve cada listado.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/api").rstrip("/")
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "ecofilia-local"
if "ecofilia.site" in BASE:
    sys.exit("Este script es para un backend local: no apunta a producción.")


def call(path, token=None, method="GET", body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def rows(data):
    return data["results"] if isinstance(data, dict) and "results" in data else (data or [])


for who in ("superadmin", "caf-admin", "caf-miembro", "otra-miembro", "publico"):
    status, tokens = call("/auth/login/", method="POST", body={"email": f"{who}@local.test", "password": PASSWORD})
    token = (tokens or {}).get("access")
    print(f"\n## {who} (login {status})")
    if not token:
        continue
    skills = rows(call("/skills/", token)[1])
    print("  skills:", sorted(f"{s['slug']} [{s['scope']}:{s['organization_slug']}]" for s in skills))
    print("  operaciones:", sorted(p["slug"] for p in rows(call("/projects/", token)[1])))
    print("  documentos:", sorted(d["slug"] for d in rows(call("/document/list/?scope=all", token)[1])))
    tags = rows(call("/document/evidence-tags/", token)[1])
    print("  etiquetas de institución:", sorted(f"{t['slug']}:{t['organization_slug']}" for t in tags if t["organization_slug"]),
          "| globales:", sum(1 for t in tags if not t["organization_slug"]))
    print("  informes emitidos:", sorted(r["project_slug"] for r in rows(call("/projects/stage-reports/emitidos/", token)[1])))
    print("  evaluation-records (sin org / caf / otra):",
          call("/projects/evaluation-records/", token)[0],
          call("/projects/evaluation-records/?organization=caf", token)[0],
          call("/projects/evaluation-records/?organization=otra", token)[0])
    print("  GET local-plataforma:", call("/skills/local-plataforma/", token)[0])
