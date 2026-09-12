#!/usr/bin/env python3
"""Serveur MCP stdio qui relaie les outils OrcaSlicer vers le plugin Orca Copilot.

Lance par `claude` (via --mcp-config), il tourne avec le python3 du systeme et
non avec l'interpreteur embarque d'OrcaSlicer. Il ne connait rien d'OrcaSlicer :
il transmet chaque appel d'outil au pont TCP loopback ouvert par le plugin.

    claude --(stdio JSON-RPC)--> ce script --(TCP 127.0.0.1)--> plugin --> orca.host

Stdlib uniquement : le plugin declare `dependencies = []` et l'installation `uv`
d'OrcaSlicer a un timeout dur de 120 s.
"""

import json
import os
import socket
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "orca", "version": "0.1.0"}

PORT = int(os.environ.get("ORCA_BRIDGE_PORT", "0"))
TOKEN = os.environ.get("ORCA_BRIDGE_TOKEN", "")
EDIT_AVAILABLE = os.environ.get("ORCA_EDIT_AVAILABLE", "0") == "1"

APPLY_DESC = (
    "Applique des reglages a la configuration active. L'utilisateur voit un diff "
    "et doit confirmer : l'appel bloque jusqu'a sa reponse et peut etre refuse. "
    "Verifie les bornes avec get_setting_metadata avant d'appeler."
)
if not EDIT_AVAILABLE:
    APPLY_DESC = ("INDISPONIBLE sur ce build d'OrcaSlicer (API en lecture seule). "
                  "N'appelle pas cet outil : presente un tableau de recommandations.")

TOOLS = [
    {
        "name": "get_settings",
        "description": (
            "Lit la configuration d'impression active (process + filament + imprimante "
            "fusionnes). Sans argument : les reglages essentiels. keys=[...] pour des "
            "cles precises, search=\"retract\" pour chercher par sous-chaine. "
            "Les valeurs sont des chaines serialisees telles qu'OrcaSlicer les stocke."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"},
                         "description": "Cles exactes a lire."},
                "search": {"type": "string",
                           "description": "Sous-chaine cherchee dans les noms de cles."},
            },
        },
    },
    {
        "name": "get_setting_metadata",
        "description": ("Libelle, type, unite, bornes min/max et valeurs d'enum d'un ou "
                        "plusieurs reglages. A consulter avant de proposer une valeur."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["keys"],
        },
    },
    {
        "name": "get_presets",
        "description": ("Presets actifs (process, filament, imprimante), leur nom, s'ils "
                        "sont modifies (dirty) ou systeme, et la liste des presets dispo."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_model_info",
        "description": ("Geometrie des objets sur le plateau : nombre d'objets, volumes, "
                        "nombre de triangles, volume en mm3 et encombrement en mm."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_settings",
        "description": APPLY_DESC,
        "inputSchema": {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "value": {"type": "string",
                                      "description": "Valeur serialisee, ex. \"0.2\" ou \"15%\"."},
                            "reason": {"type": "string",
                                       "description": "Une phrase : la cause physique visee."},
                        },
                        "required": ["key", "value"],
                    },
                },
                "preset_type": {"type": "string", "enum": ["print", "filament", "printer"],
                                "default": "print"},
            },
            "required": ["changes"],
        },
    },
]


def call_plugin(method, params):
    """Un aller-retour ligne-JSON sur le pont loopback. Une connexion par appel."""
    if not PORT or not TOKEN:
        raise RuntimeError("ORCA_BRIDGE_PORT / ORCA_BRIDGE_TOKEN absents de l'environnement")
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as sock:
        # apply_settings attend une confirmation humaine : pas de timeout court.
        sock.settimeout(360 if method == "apply_settings" else 30)
        payload = json.dumps({"token": TOKEN, "method": method, "params": params}) + "\n"
        sock.sendall(payload.encode("utf-8"))

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("le plugin a ferme la connexion")
            buf += chunk

    reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "erreur inconnue cote plugin"))
    return reply.get("result")


def write(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(req_id, result):
    write({"jsonrpc": "2.0", "id": req_id, "result": result})


def reply_error(req_id, code, message):
    write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def handle(request):
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    # Les notifications (pas d'id) ne recoivent jamais de reponse.
    if req_id is None:
        return

    if method == "initialize":
        reply(req_id, {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
        return

    if method == "tools/list":
        reply(req_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            result = call_plugin(name, args)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            reply(req_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:
            # Une erreur d'outil se renvoie dans le resultat avec isError, pas en
            # erreur JSON-RPC : le modele doit pouvoir la lire et se corriger.
            reply(req_id, {
                "content": [{"type": "text", "text": "Erreur: %s" % exc}],
                "isError": True,
            })
        return

    if method in ("ping",):
        reply(req_id, {})
        return

    reply_error(req_id, -32601, "methode inconnue: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            continue
        try:
            handle(request)
        except Exception as exc:
            req_id = request.get("id")
            if req_id is not None:
                reply_error(req_id, -32603, str(exc))


if __name__ == "__main__":
    main()
