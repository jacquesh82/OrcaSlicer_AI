# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Orca Copilot"
# description = "Assistant IA de configuration d'impression, propulse par Claude Code (souscription)."
# author = "Jacques"
# version = "0.1.0"
# ///
"""Orca Copilot - un chatbot agentique dans OrcaSlicer.

Architecture
------------
    page HTML (webview)                     plugin (CPython embarque)
        |  orca.postMessage(..)  ------->   on_message()      [thread UI]
        |  orca.onMessage(cb)    <-------   win.post(..)      [n'importe quel thread]

    on_message() [thread UI] fige un instantane de la configuration en dicts simples,
    puis delegue l'attente a un thread : le thread ne touche JAMAIS orca.host.

    claude (sous-processus) --stdio--> orca_mcp_bridge.py --TCP loopback--> BridgeServer
                                                                              |
    Les outils de lecture repondent depuis l'instantane. apply_settings, lui, doit
    s'executer sur le thread UI : il fait un aller-retour par la page (demande de
    confirmation), ce qui sert a la fois de garde-fou et de marshaling vers le thread UI.

L'ecriture des reglages (orca.host.edit) n'existe que sur un build patche. Sur un
OrcaSlicer standard le plugin fonctionne quand meme, en mode recommandation seule.
"""

import json
import math
import os
import queue
import secrets
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import traceback

import orca

PLUGIN_VERSION = "0.1.0"
BRIDGE_FILENAME = "orca_mcp_bridge.py"

# Capabilities vivantes : Chat et Auto partagent un seul panneau/pont.
_LIVE_COPILOTS = []

# Demande initiale du mode Automatique (bouton « AI optimisation » de la barre).
AUTO_PROMPT = (
    "Analyse la piece sur le plateau et ma configuration pour une meilleure impression. "
    "Commence par get_slicing_status : s'il y a des warnings (level >= 1) ou une erreur, "
    "leur resolution passe AVANT toute optimisation. Utilise get_model_info pour la "
    "geometrie et get_settings pour la configuration, consulte get_setting_metadata avant "
    "toute proposition. Cherche les reglages sous-optimaux (suretes, supports, temps, "
    "temperature, vitesses...) et propose les correctifs via apply_settings, chacun "
    "justifie par une cause physique. Apres chaque apply, verifie le nouveau verdict "
    "via get_slicing_status (max 3 boucles). S'il n'y a rien d'ameliorable, dis-le."
)

# Objectifs presents a l'utilisateur avant l'automatisation : il les reordonne
# (les fleches), le choix pondere toutes les decisions de l'agent.
AUTO_OBJECTIVES = [
    "Fiabilite (eliminer warnings et echecs)",
    "Qualite d'impression",
    "Rapidite (temps d'impression)",
    "Moins de supports (matiere, nettoyage)",
    "Economie de filament",
    "Simplicite (rester proche des presets)",
]


def _auto_prompt_with_priorities(priorities, note=""):
    prompt = AUTO_PROMPT + "\n\nPriorites de l'utilisateur, par ordre decroissant :"
    for rank, item in enumerate(priorities or AUTO_OBJECTIVES, start=1):
        prompt += "\n%d. %s" % (rank, item)
    prompt += ("\nPese chaque decision selon cet ordre : un gain sur un objectif "
               "bas ne vaut jamais une perte sur un objectif haut.")
    if note and note.strip():
        prompt += "\n\nPrecision de l'utilisateur : " + note.strip()
    return prompt

# Suite des instructions affichee a l'ouverture du mode Chatbot.
WELCOME_TEXT = (
    "Pret. Decris ton probleme (« j'ai du stringing sur cette piece ») ou demande "
    "une revue des reglages.\n"
    "- Pastille « ecriture autorisee » : le Copilot peut appliquer (carte a cocher, "
    "confirmation obligatoire, Ctrl+Z possible).\n"
    "- Pastille « permission refusee » : active « Allow modifying settings » pour ce "
    "plugin dans Fichier > Plugins, puis rouvre le Copilot.\n"
    "- Pastille « lecture seule » : build non patche, recommandations seulement."
)

# Les exceptions levees dans un thread que le plugin cree lui-meme ne remontent
# ni en C++ ni dans un dialogue : elles ne sortent que dans python_*.log. On les
# rattrape donc systematiquement et on les renvoie dans le chat.
def _log(*parts):
    print("[orca-copilot]", *parts, flush=True)


# --------------------------------------------------------------------------
# Instantanes de la configuration - a n'appeler QUE depuis le thread UI
# --------------------------------------------------------------------------

# Les cles reellement utiles a un diagnostic d'impression. On ne les impose pas :
# elles sont filtrees contre les cles reellement presentes, donc une cle absente
# d'une version d'OrcaSlicer est simplement ignoree.
ESSENTIAL_KEYS = [
    # couches / parois
    "layer_height", "initial_layer_print_height", "wall_loops", "wall_generator",
    "top_shell_layers", "bottom_shell_layers", "top_shell_thickness", "bottom_shell_thickness",
    # remplissage
    "sparse_infill_density", "sparse_infill_pattern", "infill_direction",
    "top_surface_pattern", "bottom_surface_pattern",
    # temperatures
    "nozzle_temperature", "nozzle_temperature_initial_layer",
    "hot_plate_temp", "hot_plate_temp_initial_layer",
    "cool_plate_temp", "cool_plate_temp_initial_layer",
    "textured_plate_temp", "eng_plate_temp", "chamber_temperature",
    # matiere
    "filament_type", "filament_flow_ratio", "filament_diameter",
    "pressure_advance", "enable_pressure_advance",
    # retraction (la cle du stringing)
    "retraction_length", "retract_speed", "deretract_speed",
    "retraction_minimum_travel", "retract_when_changing_layer",
    "z_hop", "z_hop_types", "wipe", "wipe_distance",
    "retract_before_wipe",
    # vitesses
    "travel_speed", "outer_wall_speed", "inner_wall_speed", "sparse_infill_speed",
    "internal_solid_infill_speed", "top_surface_speed", "initial_layer_speed",
    "initial_layer_infill_speed", "overhang_1_4_speed", "bridge_speed",
    "gap_infill_speed", "default_acceleration", "outer_wall_acceleration",
    # refroidissement
    "fan_min_speed", "fan_max_speed", "fan_cooling_layer_time",
    "overhang_fan_speed", "overhang_fan_threshold", "enable_overhang_bridge_fan",
    "close_fan_the_first_x_layers", "slow_down_layer_time", "slow_down_min_speed",
    "reduce_fan_stop_start_freq",
    # adherence (la cle du warping)
    "brim_type", "brim_width", "brim_object_gap", "skirt_loops", "skirt_distance",
    "elefant_foot_compensation", "raft_layers",
    # supports
    "enable_support", "support_type", "support_threshold_angle",
    "support_top_z_distance", "support_bottom_z_distance", "support_base_pattern",
    # divers
    "print_sequence", "seam_position", "detect_thin_wall", "xy_hole_compensation",
    "xy_contour_compensation", "line_width", "inner_wall_line_width",
    "outer_wall_line_width", "initial_layer_line_width",
    # machine
    "printer_model", "nozzle_diameter", "printable_area", "printable_height",
    "machine_max_acceleration_x", "machine_max_speed_x",
]


def _has_edit_api():
    """orca.host.edit n'existe que sur un build patche (PATCH B)."""
    return hasattr(orca.host, "edit")


def _can_write():
    """Trois etats a distinguer, et l'UI doit les dire differemment :
    build standard (pas d'API), API presente mais permission refusee, permission accordee.
    """
    if not _has_edit_api():
        return False
    try:
        return bool(orca.host.edit.can_write())
    except Exception:
        return False


def snapshot_settings():
    """Toute la configuration fusionnee active, sous forme de dict {cle: str}.

    Les valeurs remontent serialisees en chaines ("0.2", "15%", "1,1,1"), jamais
    en types Python : c'est le contrat de full_config_value().
    """
    bundle = orca.host.preset_bundle()
    out = {}
    for key in bundle.full_config_keys():
        try:
            value = bundle.full_config_value(key)
        except Exception:
            continue
        if value is not None:
            out[key] = value
    return out


def snapshot_presets():
    bundle = orca.host.preset_bundle()
    out = {}
    for attr, label in (("prints", "process"), ("printers", "printer"), ("filaments", "filament")):
        try:
            coll = getattr(bundle, attr)
            edited = coll.edited_preset()
            out[label] = {
                # selected = tel qu'enregistre sur disque ; edited = selected + modifications non sauvees.
                "selected_name": coll.selected_preset_name(),
                "edited_name": edited.name,
                "is_dirty": bool(edited.is_dirty),
                "is_system": bool(edited.is_system),
                "available": list(coll.preset_names())[:200],
            }
        except Exception as exc:
            out[label] = {"error": str(exc)}
    try:
        out["filaments_in_use"] = list(bundle.current_filament_preset_names())
    except Exception:
        pass
    return out


def snapshot_model():
    """Geometrie de la scene : ce qui permet a l'agent d'adapter ses conseils a la piece."""
    try:
        model = orca.host.model()
    except RuntimeError as exc:
        return {"error": str(exc), "objects": []}

    objects = []
    for index, obj in enumerate(model.objects()):
        entry = {"index": index, "name": getattr(obj, "name", ""), "volumes": [], "instances": 0}
        try:
            entry["instances"] = len(obj.instances())
        except Exception:
            pass
        try:
            # Rotations absolues des instances, en degres : c'est la reference
            # pour cibler rotate_objects (rotation relative).
            entry["instance_rotations_deg"] = [
                [round(math.degrees(float(a)), 1) for a in inst.rotation()]
                for inst in obj.instances()
            ]
        except Exception:
            pass
        try:
            for vol in obj.volumes():
                vinfo = {"name": getattr(vol, "name", "")}
                try:
                    mesh = vol.mesh()
                    vinfo["triangles"] = mesh.triangle_count()
                    vinfo["vertices"] = mesh.vertex_count()
                    vinfo["volume_mm3"] = round(float(mesh.volume()), 2)
                    bbox = mesh.bounding_box()
                    if bbox.defined:
                        vinfo["size_mm"] = [round(float(c), 2) for c in bbox.size]
                except Exception as exc:
                    vinfo["mesh_error"] = str(exc)
                entry["volumes"].append(vinfo)
        except Exception as exc:
            entry["error"] = str(exc)
        objects.append(entry)
    return {"objects": objects, "object_count": len(objects)}


def setting_metadata(keys):
    """Libelle / bornes / enum des reglages. Necessite PATCH B ; degrade proprement.

    Le binding prend la liste entiere en un appel et renvoie {cle: metadonnees}.
    """
    fn = getattr(orca.host, "setting_metadata", None)
    if fn is None:
        return {"unavailable": "orca.host.setting_metadata absent de ce build "
                               "(PATCH B non applique) - valeurs a proposer avec prudence."}
    try:
        return dict(fn(list(keys)))
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


# --------------------------------------------------------------------------
# Pont loopback : le serveur MCP stdio lance par claude vient taper ici
# --------------------------------------------------------------------------

class _BridgeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        for raw in self.rfile:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except Exception as exc:
                self._send({"ok": False, "error": "json invalide: %s" % exc})
                continue
            if req.get("token") != server.token:
                self._send({"ok": False, "error": "jeton invalide"})
                continue
            try:
                result = server.dispatch(req.get("method", ""), req.get("params") or {})
                self._send({"ok": True, "result": result})
            except Exception as exc:
                _log("erreur outil:", traceback.format_exc())
                self._send({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})

    def _send(self, payload):
        try:
            self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass


class BridgeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, copilot):
        super().__init__(("127.0.0.1", 0), _BridgeHandler)
        self.copilot = copilot
        self.token = secrets.token_urlsafe(24)

    @property
    def port(self):
        return self.server_address[1]

    def dispatch(self, method, params):
        return self.copilot.handle_tool(method, params)


# --------------------------------------------------------------------------
# Session claude : un sous-processus long, pilote en stream-json
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """Tu es un expert du reglage d'imprimantes 3D FDM, integre dans OrcaSlicer.

Tu disposes d'outils MCP (prefixe mcp__orca__) branches sur la session OrcaSlicer en cours :
- get_settings : lis la configuration active. Sans argument tu recois les reglages
  essentiels ; utilise keys=[...] ou search="..." pour aller chercher le reste.
- get_setting_metadata : bornes, type et enum d'un reglage. Consulte-le AVANT de
  proposer une valeur, pour ne jamais proposer une valeur hors bornes.
- get_presets : presets actifs (process / filament / imprimante) et leur etat.
- get_model_info : geometrie de la piece chargee sur le plateau (indices, rotation).
- apply_settings : applique des reglages. L'utilisateur voit un diff et confirme.
- rotate_objects : fait pivoter des objets du plateau (degres relatifs par axe).
  objects=null pivote TOUT le plateau ; sinon liste d'indices (get_model_info).
  L'utilisateur voit une carte et confirme.
- get_slicing_status : verdict VIF du slicer — etat, erreur bloquante, warnings
  (level 0=info, 1=warning, 2=error).

Resolution automatique des warnings : apres CHAQUE apply_settings (qui declenche
un reslice), rappelle get_slicing_status jusqu'a state 'completed'. Si des
warnings de level >= 1 ou une erreur subsistent, diagnostique-les et propose le
correctif suivant via apply_settings. Boucle au plus 3 corrections d'affilee,
puis fais le point a l'utilisateur. Si l'etat reste 'slicing', re-interroge
(plutot que d'attendre aveuglement). Ne propose jamais un correctif dont tu ne
connais pas la cause physique.

Methode de travail :
1. Commence par LIRE la configuration reelle avant de conclure. Ne devine jamais une
   valeur que tu peux aller chercher.
2. Raisonne a partir des symptomes decrits et des valeurs observees.
3. Propose peu de changements a la fois, chacun justifie par une cause physique.
4. Chaque valeur doit etre coherente avec les metadonnees du reglage.

Quand tu conclus a des changements de reglages, tu les APPLIQUES, tu ne les decris pas :
appelle apply_settings avec changes=[{key, value, reason}] (reason : une ligne, la
cause physique). L'utilisateur recoit une carte avec un bouton « Appliquer les
changements proposes » et peut decocher des lignes. Ta reponse texte ne repete PAS
la liste des changements — elle donne le diagnostic et les justifications ; le
diff s'affiche deja a cote. Si apply_settings echoue (lecture seule ou permission
refusee), alors seulement presente tes recommandations en tableau markdown.

Les valeurs sont des chaines serialisees, exactement comme OrcaSlicer les stocke
("0.2", "15%", "220"). Renvoie-les dans ce format.

Reponds en francais, de facon concise et concrete. Pas de laius : le diagnostic,
la cause, le reglage."""


class ClaudeSession:
    """Un sous-processus `claude -p` en mode stream-json, alimente en continu."""

    def __init__(self, copilot, bridge_port, bridge_token, model=None, extra_prompt=""):
        self.copilot = copilot
        self.proc = None
        self.reader = None
        self._lock = threading.Lock()

        plugin_dir = _plugin_dir()
        bridge_path = os.path.join(plugin_dir, BRIDGE_FILENAME)
        if not os.path.exists(bridge_path):
            raise RuntimeError("pont MCP introuvable: %s" % bridge_path)

        mcp_config = {
            "mcpServers": {
                "orca": {
                    "type": "stdio",
                    "command": _system_python(),
                    "args": [bridge_path],
                    "env": {
                        "ORCA_BRIDGE_PORT": str(bridge_port),
                        "ORCA_BRIDGE_TOKEN": bridge_token,
                        "ORCA_EDIT_AVAILABLE": "1" if copilot._write_ok else "0",
                    },
                }
            }
        }

        system_prompt = SYSTEM_PROMPT
        if extra_prompt:
            system_prompt += "\n\nContexte materiel fourni par l'utilisateur :\n" + extra_prompt
        if not copilot._write_ok:
            system_prompt += ("\n\nIMPORTANT : ce build d'OrcaSlicer est en lecture seule, "
                              "apply_settings echouera. Presente tes recommandations sous "
                              "forme de tableau reglage / valeur actuelle / valeur conseillee.")

        cmd = [
            _claude_bin(),
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--mcp-config", json.dumps(mcp_config),
            "--allowedTools",
            "mcp__orca__get_settings", "mcp__orca__get_setting_metadata",
            "mcp__orca__get_presets", "mcp__orca__get_model_info",
            "mcp__orca__apply_settings", "mcp__orca__rotate_objects",
            "mcp__orca__get_slicing_status",
            # --restricted retire Bash, les outils qui executent du code et WebFetch.
            # Il est incompatible avec --permission-mode bypassPermissions ("not
            # supported in restricted mode") : --allowedTools suffit a pre-autoriser
            # nos outils, tout le reste est hors de portee de l'agent.
            "--restricted",
            "--append-system-prompt", system_prompt,
        ]
        if model:
            cmd += ["--model", model]

        _log("lancement:", " ".join(cmd[:6]), "...")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=plugin_dir,
        )
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    # -- entree ------------------------------------------------------------
    def send(self, text):
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("la session claude est arretee")
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        with self._lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    # -- sortie ------------------------------------------------------------
    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                self._handle_event(event)
        except Exception:
            _log("boucle de lecture:", traceback.format_exc())
        finally:
            self.copilot.post({"command": "session_ended"})

    def _handle_event(self, event):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.copilot.post({"command": "ready",
                               "tools": event.get("tools", []),
                               "model": event.get("model", "")})
            return
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    self.copilot.post({"command": "assistant", "text": block["text"]})
                elif btype == "tool_use":
                    self.copilot.post({"command": "tool_use",
                                       "name": block.get("name", ""),
                                       "input": block.get("input", {})})
            return
        if kind == "result":
            self.copilot.post({"command": "turn_done",
                               "is_error": bool(event.get("is_error")),
                               "cost": event.get("total_cost_usd"),
                               "duration_ms": event.get("duration_ms")})
            return

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                if line.strip():
                    _log("claude stderr:", line.rstrip())
        except Exception:
            pass

    def stop(self):
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


def _plugin_dir():
    try:
        return orca.host.plugin.storage()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def _find_claude_bin():
    """Chemin de l'executable claude, ou None s'il n'est pas installe."""
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
    if os.name == "nt":
        candidates = (os.environ.get("ORCA_CLAUDE_BIN"),
                      os.path.join(local_bin, "claude.exe"),
                      shutil.which("claude") or shutil.which("claude.exe"))
    else:
        candidates = (os.environ.get("ORCA_CLAUDE_BIN"),
                      os.path.join(local_bin, "claude"),
                      "/usr/local/bin/claude",
                      "/usr/bin/claude",
                      shutil.which("claude"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _claude_bin():
    found = _find_claude_bin()
    return found if found else "claude"


CLAUDE_MANUAL_HINT = (
    "Installation automatique de claude-code impossible. A la main, dans un terminal :\n"
    "  curl -fsSL https://claude.ai/install.sh | bash\n"
    "  (Windows, PowerShell : irm https://claude.ai/install.ps1 | iex)\n"
    "ou, avec Node.js >= 18 :\n"
    "  npm install -g @anthropic-ai/claude-code\n"
    "Puis relance le Copilot."
)


def _install_claude():
    """Installe claude-code. Tourne dans un thread du plugin, hors audit
    (angle mort documente : le reseau d'un thread plugin ne declenche aucune
    demande d'autorisation). Essaie l'installateur officiel puis npm.
    Retourne (ok, detail)."""
    if os.name == "nt":
        candidates = [
            ["powershell", "-NoProfile", "-Command",
             "irm https://claude.ai/install.ps1 | iex"],
            ["npm", "install", "-g", "@anthropic-ai/claude-code"],
        ]
    else:
        candidates = [
            ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
            ["npm", "install", "-g", "@anthropic-ai/claude-code"],
        ]
    for cmd in candidates:
        try:
            subprocess.run(cmd, capture_output=True, timeout=900)
        except Exception:
            continue
        if _find_claude_bin() is not None:
            return True, "installe via %s" % cmd[0]
    return False, CLAUDE_MANUAL_HINT


def _system_python():
    """Interpreteur systeme pour lancer le pont MCP (hors OrcaSlicer)."""
    if os.name == "nt":
        # Sur Windows l'executable est python.exe ; le lanceur py -3 imposerait
        # un argument supplementaire que la commande MCP ne decoupe pas.
        return "python"
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(candidate):
            return candidate
    return "python3"


# --------------------------------------------------------------------------
# La page. Autonome : pas de CSP, pas de devtools, aucune ressource externe.
# L'hote injecte un theme avant le rendu -> on ne peint qu'avec ses variables.
# --------------------------------------------------------------------------

PAGE = r"""
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    background: var(--orca-bg); color: var(--orca-fg);
    font-family: var(--orca-font, system-ui, sans-serif); font-size: 13px;
  }
  header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; border-bottom: 1px solid var(--orca-border);
    flex: 0 0 auto;
  }
  header h1 { font-size: 13px; font-weight: 600; margin: 0; }
  .pill {
    font-size: 11px; padding: 2px 8px; border-radius: 999px;
    border: 1px solid var(--orca-border); color: var(--orca-muted);
  }
  .pill.write { border-color: var(--orca-accent); color: var(--orca-accent); }
  #log { flex: 1 1 auto; overflow-y: auto; padding: 14px; }
  .msg { margin-bottom: 14px; max-width: 92%; }
  .msg.user { margin-left: auto; }
  .msg .who { font-size: 11px; color: var(--orca-muted); margin-bottom: 3px; }
  .msg.user .who { text-align: right; }
  .bubble {
    padding: 9px 12px; border-radius: 10px;
    border: 1px solid var(--orca-border); line-height: 1.5;
  }
  .msg.user .bubble { white-space: pre-wrap; }
  /* rendu markdown des reponses de l'agent */
  .bubble h1, .bubble h2, .bubble h3, .bubble h4 { margin: 10px 0 4px; font-size: 13px; }
  .bubble h1:first-child, .bubble h2:first-child, .bubble h3:first-child { margin-top: 0; }
  .bubble p { margin: 0 0 8px; }
  .bubble p:last-child { margin-bottom: 0; }
  .bubble ul, .bubble ol { margin: 0 0 8px; padding-left: 18px; }
  .bubble li { margin: 2px 0; }
  .bubble code {
    font-family: ui-monospace, monospace; font-size: 12px;
    background: color-mix(in srgb, var(--orca-border) 45%, transparent);
    padding: 1px 4px; border-radius: 4px;
  }
  .bubble pre {
    font-family: ui-monospace, monospace; font-size: 12px; overflow-x: auto;
    background: color-mix(in srgb, var(--orca-border) 30%, transparent);
    padding: 8px; border-radius: 6px; margin: 0 0 8px;
  }
  .bubble pre code { background: transparent; padding: 0; }
  .bubble a { text-decoration: underline; }
  .bubble strong { font-weight: 600; }
  .msg.user .bubble { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: transparent; }
  .tool {
    font-size: 11px; color: var(--orca-muted); margin-bottom: 8px;
    font-family: ui-monospace, monospace;
  }
  .err .bubble { border-color: #d9534f; color: #d9534f; }
  .diff { border: 1px solid var(--orca-accent); border-radius: 10px; padding: 12px; margin-bottom: 14px; }
  .diff h3 { margin: 0 0 8px; font-size: 12px; }
  .diff .row-check { accent-color: var(--orca-accent); }
  /* cadrage du mode automatique : liste d'objectifs ordonnable */
  .prio-list { margin: 8px 0; }
  .prio-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
  .prio-rank { color: var(--orca-muted); min-width: 18px; text-align: right; font-size: 12px; }
  .prio-name { flex: 1; font-size: 12px; }
  .prio-row button {
    padding: 2px 8px; font-size: 12px; border-radius: 6px;
  }
  .prio-note { width: 100%; margin-top: 6px; height: 40px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--orca-border); vertical-align: top; }
  th { color: var(--orca-muted); font-weight: 500; }
  td.key { font-family: ui-monospace, monospace; }
  td.new { color: var(--orca-accent); font-weight: 600; }
  .why { color: var(--orca-muted); font-size: 11px; }
  .actions { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  button {
    font: inherit; padding: 6px 14px; border-radius: 7px; cursor: pointer;
    border: 1px solid var(--orca-border); background: transparent; color: var(--orca-fg);
  }
  button.primary { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: transparent; }
  button:disabled { opacity: .45; cursor: default; }
  footer { flex: 0 0 auto; border-top: 1px solid var(--orca-border); padding: 10px; display: flex; gap: 8px; }
  textarea {
    flex: 1; resize: none; height: 56px; padding: 8px; border-radius: 8px;
    border: 1px solid var(--orca-border); background: transparent;
    color: var(--orca-fg); font: inherit;
  }
  .hint { padding: 0 14px 10px; color: var(--orca-muted); font-size: 11px; }
  /* barre d'attente animee (agent au travail, installation de claude-code) */
  .busy {
    flex: 0 0 auto; display: flex; align-items: center; gap: 10px;
    padding: 9px 14px; border-top: 1px solid var(--orca-border);
    color: var(--orca-muted); font-size: 12px;
  }
  .busy[hidden] { display: none; }
  .busy-bar {
    position: relative; width: 90px; height: 3px; flex: 0 0 auto;
    overflow: hidden; border-radius: 2px; background: var(--orca-border);
  }
  .busy-slide {
    position: absolute; top: 0; left: -40%; width: 40%; height: 100%;
    border-radius: 2px; background: var(--orca-accent);
    animation: busy-slide 1.1s ease-in-out infinite;
  }
  @keyframes busy-slide { 0% { left: -40%; } 100% { left: 100%; } }
</style>

<header>
  <h1>Orca Copilot</h1>
  <span class="pill" id="mode">demarrage...</span>
  <span style="flex:1"></span>
  <span class="pill" id="stat"></span>
</header>

<div id="log"></div>
<div class="hint" id="hint">
  Decris ton probleme (« j'ai du stringing sur cette piece ») ou demande une revue des reglages.
</div>
<div class="busy" id="busy" hidden>
  <div class="busy-bar"><div class="busy-slide"></div></div>
  <span id="busyText">...</span>
</div>

<footer>
  <textarea id="input" placeholder="Entree pour envoyer, Maj+Entree pour une nouvelle ligne"></textarea>
  <button class="primary" id="send">Envoyer</button>
</footer>

<script>
(function () {
  var log = document.getElementById('log');
  var input = document.getElementById('input');
  var send = document.getElementById('send');
  var mode = document.getElementById('mode');
  var stat = document.getElementById('stat');
  var hint = document.getElementById('hint');
  var busyBar = document.getElementById('busy');
  var busyText = document.getElementById('busyText');
  var busy = false;

  function scroll() { log.scrollTop = log.scrollHeight; }

  // Messages des etapes : la barre d'attente dit ou en est l'agent.
  var TOOL_LABELS = {
    get_settings: 'Lecture de la configuration',
    get_setting_metadata: 'Consultation des bornes des reglages',
    get_presets: 'Lecture des presets',
    get_model_info: 'Analyse de la piece',
    apply_settings: 'Preparation des propositions'
  };

  function showBusy(text) {
    hint.hidden = true;
    busyBar.hidden = false;
    if (text) busyText.textContent = text;
    scroll();
  }
  function hideBusy() {
    busyBar.hidden = true;
    busyText.textContent = '...';
  }

  function setBusy(v) {
    busy = v;
    send.disabled = v;
    stat.textContent = v ? 'reflexion...' : '';
    if (v) showBusy('Le Copilot reflechit...');
    else hideBusy();
  }

  // Mini markdown -> HTML pour les reponses de l'agent (titres, gras, italique,
  // code, listes, liens). L'echappement HTML passe d'abord : le rendu ne peut
  // jamais reinjecter du markup cru venu du texte.
  function esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function inlineMd(s) {
    return s
      .replace(/`([^`]+)`/g, function (_, c) { return '<code>' + c + '</code>'; })
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
               function (_, t, u) { return '<a href="' + u + '" target="_blank">' + t + '</a>'; });
  }
  function mdToHtml(text) {
    var lines = esc(String(text)).split(/\r?\n/);
    var out = [], inList = null, inPre = false, para = [];
    function flushPara() {
      if (para.length) { out.push('<p>' + inlineMd(para.join('<br>')) + '</p>'); para = []; }
    }
    function flushList() {
      if (inList) { out.push('</' + inList + '>'); inList = null; }
    }
    lines.forEach(function (raw) {
      var line = raw;
      if (inPre) {
        if (/^```/.test(line.trim())) { out.push('</pre></code>'); inPre = false; }
        else out.push(line);
        return;
      }
      var t = line.trim();
      if (/^```/.test(t)) {
        flushPara(); flushList();
        out.push('<pre><code>'); inPre = true; return;
      }
      var h = t.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flushPara(); flushList();
        var lvl = h[1].length;
        out.push('<h' + lvl + '>' + inlineMd(h[2]) + '</h' + lvl + '>'); return;
      }
      var ul = t.match(/^[-*]\s+(.*)$/);
      var ol = t.match(/^\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        var want = ul ? 'ul' : 'ol';
        if (inList !== want) { flushList(); out.push('<' + want + '>'); inList = want; }
        out.push('<li>' + inlineMd((ul || ol)[1]) + '</li>'); return;
      }
      if (t === '') { flushPara(); flushList(); return; }
      para.push(line);
    });
    if (inPre) out.push('</code></pre>');
    flushPara(); flushList();
    return out.join('\n') || '<p></p>';
  }

  function bubble(who, text, cls) {
    var d = document.createElement('div');
    d.className = 'msg ' + (cls || '');
    var h = document.createElement('div');
    h.className = 'who'; h.textContent = who;
    var b = document.createElement('div');
    b.className = 'bubble';
    if (cls === 'user') b.textContent = text;
    else b.innerHTML = mdToHtml(text);
    d.appendChild(h); d.appendChild(b); log.appendChild(d); scroll();
    return b;
  }

  function toolLine(name, input) {
    var d = document.createElement('div');
    d.className = 'tool';
    var short = String(name).replace('mcp__orca__', '');
    var arg = '';
    try {
      var keys = Object.keys(input || {});
      if (keys.length) arg = ' ' + JSON.stringify(input).slice(0, 120);
    } catch (e) {}
    d.textContent = '⚙ ' + short + arg;
    log.appendChild(d); scroll();
    // La barre d'attente suit l'etape en cours.
    if (busy) showBusy(TOOL_LABELS[short] || ('Outil ' + short + '...'));
  }

  // Carte de diff : rien ne s'ecrit dans les presets sans un clic ici. Chaque
  // ligne est decochable ; seules les lignes cochees seront appliquees.
  function askApply(req) {
    var box = document.createElement('div');
    box.className = 'diff';
    var h = document.createElement('h3');
    h.textContent = 'Changements proposes (' + req.changes.length + ')';
    box.appendChild(h);

    var t = document.createElement('table');
    t.innerHTML = '<tr><th></th><th>Reglage</th><th>Actuel</th><th>Propose</th></tr>';
    var rows = [];
    req.changes.forEach(function (c) {
      var tr = document.createElement('tr');
      var td0 = document.createElement('td');
      var cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = true; cb.className = 'row-check';
      td0.appendChild(cb);
      var td1 = document.createElement('td'); td1.className = 'key';
      td1.textContent = c.key;
      if (c.reason) {
        var w = document.createElement('div'); w.className = 'why';
        w.textContent = c.reason; td1.appendChild(w);
      }
      var td2 = document.createElement('td'); td2.textContent = c.current === null ? '-' : c.current;
      var td3 = document.createElement('td'); td3.className = 'new'; td3.textContent = c.proposed;
      tr.appendChild(td0); tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
      t.appendChild(tr);
      rows.push({ key: c.key, cb: cb, tr: tr });
    });
    box.appendChild(t);

    var actions = document.createElement('div');
    actions.className = 'actions';
    var yes = document.createElement('button');
    yes.className = 'primary'; yes.textContent = 'Appliquer les changements proposes';
    var no = document.createElement('button');
    no.textContent = 'Refuser';
    function answer(ok) {
      var keys = rows.filter(function (r) { return r.cb.checked; })
                     .map(function (r) { return r.key; });
      yes.disabled = no.disabled = true;
      rows.forEach(function (r) { r.cb.disabled = true; if (!r.cb.checked) r.tr.style.opacity = .45; });
      var verdict = document.createElement('span');
      verdict.className = 'why';
      verdict.textContent = ok
        ? (' applique (' + keys.length + '/' + rows.length + ')')
        : ' refuse';
      actions.appendChild(verdict);
      window.orca.postMessage({
        command: 'apply_response', id: req.id, approved: ok, keys: keys
      });
    }
    yes.onclick = function () { answer(true); };
    no.onclick = function () { answer(false); };
    actions.appendChild(yes); actions.appendChild(no);
    box.appendChild(actions);
    log.appendChild(box); scroll();
  }

  // Carte de rotation : meme contrat que la carte de reglages.
  function askRotate(req) {
    var box = document.createElement('div');
    box.className = 'diff';
    var h = document.createElement('h3');
    var axes = ['x', 'y', 'z']
      .filter(function (a) { return (req.rotation || {})[a]; })
      .map(function (a) { return req.rotation[a] + '\u00b0 (' + a + ')'; })
      .join(', ');
    var cible = req.objects && req.objects.length
      ? req.objects.length + ' objet(s) (' + req.objects.join(', ') + ')'
      : 'tous les objets du plateau';
    h.textContent = 'Faire pivoter ' + cible + ' de ' + axes + ' ?';
    box.appendChild(h);
    if (req.reason) {
      var w = document.createElement('div'); w.className = 'why';
      w.textContent = req.reason; box.appendChild(w);
    }
    if (req.drop_to_bed) {
      var d = document.createElement('div'); d.className = 'why';
      d.textContent = 'Les objets pivotes seront reposes sur le plateau.';
      box.appendChild(d);
    }

    var actions = document.createElement('div');
    actions.className = 'actions';
    var yes = document.createElement('button');
    yes.className = 'primary'; yes.textContent = 'Pivoter';
    var no = document.createElement('button');
    no.textContent = 'Refuser';
    function answer(ok) {
      yes.disabled = no.disabled = true;
      var verdict = document.createElement('span');
      verdict.className = 'why';
      verdict.textContent = ok ? ' pivote' : ' refuse';
      actions.appendChild(verdict);
      window.orca.postMessage({ command: 'rotate_response', id: req.id, approved: ok });
    }
    yes.onclick = function () { answer(true); };
    no.onclick = function () { answer(false); };
    actions.appendChild(yes); actions.appendChild(no);
    box.appendChild(actions);
    log.appendChild(box); scroll();
  }

  // Cadrage du mode Automatique : l'utilisateur ordonne ses objectifs avant
  // que l'agent ne parte. L'ordre choisi pondere toutes les decisions.
  function askAutoSetup(req) {
    var box = document.createElement('div');
    box.className = 'diff';
    var h = document.createElement('h3');
    h.textContent = 'Priorites de l\u2019optimisation (ordre decroissant)';
    box.appendChild(h);
    var intro = document.createElement('div');
    intro.className = 'why';
    intro.textContent = 'Reordonne avec les fleches, puis lance. Le haut de la liste pese le plus lourd.';
    box.appendChild(intro);

    var list = document.createElement('div');
    var items = (req.objectives || []).map(function (label) {
      var row = document.createElement('div');
      row.className = 'prio-row';
      var rank = document.createElement('span');
      rank.className = 'prio-rank';
      var name = document.createElement('span');
      name.className = 'prio-name'; name.textContent = label;
      var up = document.createElement('button'); up.textContent = '\u2191';
      var down = document.createElement('button'); down.textContent = '\u2193';
      function refresh() {
        var rows = list.querySelectorAll('.prio-row');
        for (var i = 0; i < rows.length; i++)
          rows[i].querySelector('.prio-rank').textContent = (i + 1) + '.';
      }
      up.onclick = function () {
        var prev = row.previousElementSibling;
        if (prev) { list.insertBefore(row, prev); refresh(); }
      };
      down.onclick = function () {
        var next = row.nextElementSibling;
        if (next) { list.insertBefore(next, row); refresh(); }
      };
      row.appendChild(rank); row.appendChild(name); row.appendChild(up); row.appendChild(down);
      list.appendChild(row);
      return row;
    });
    box.appendChild(list);
    (function refresh() {
      var rows = list.querySelectorAll('.prio-row');
      for (var i = 0; i < rows.length; i++)
        rows[i].querySelector('.prio-rank').textContent = (i + 1) + '.';
    })();

    var note = document.createElement('textarea');
    note.className = 'prio-note';
    note.placeholder = 'Precision optionnelle (materiau, contrainte, deadline...)';

    var actions = document.createElement('div');
    actions.className = 'actions';
    var go = document.createElement('button');
    go.className = 'primary'; go.textContent = 'Lancer l\u2019analyse';
    go.onclick = function () {
      go.disabled = true;
      var priorities = [];
      list.querySelectorAll('.prio-name').forEach(function (n) { priorities.push(n.textContent); });
      window.orca.postMessage({ command: 'auto_start', priorities: priorities,
                                note: note.value || '' });
    };
    actions.appendChild(go);
    box.appendChild(note);
    box.appendChild(actions);
    log.appendChild(box); scroll();
    box.scrollIntoView({ behavior: 'smooth' });
  }

  window.orca.onMessage(function (m) {
    if (!m || !m.command) return;
    switch (m.command) {
      case 'ready':
        if (m.write) {
          mode.textContent = 'ecriture autorisee';
          mode.className = 'pill write';
        } else {
          mode.textContent = m.has_api ? 'permission refusee' : 'lecture seule';
          mode.className = 'pill';
        }
        break;
      case 'assistant':
        bubble('Copilot', m.text);
        break;
      case 'tool_use':
        toolLine(m.name, m.input);
        break;
      case 'turn_done':
        setBusy(false);
        break;
      case 'error':
        bubble('Erreur', m.text, 'err');
        setBusy(false);
        break;
      case 'info':
        bubble('', m.text);
        break;
      case 'busy':
        // Attente pilotee par le plugin (installation de claude-code, analyse
        // automatique) : anime tant que la suite n'arrive pas.
        if (m.active === false) hideBusy();
        else { busy = true; send.disabled = true; showBusy(m.text || 'En cours...'); }
        break;
      case 'apply_request':
        askApply(m);
        break;
      case 'rotate_request':
        askRotate(m);
        break;
      case 'auto_setup':
        askAutoSetup(m);
        break;
      case 'claude_status':
        hideBusy();
        if (m.ok) {
          bubble('Copilot', 'claude-code installe. Demarrage...');
          window.orca.postMessage({ command: 'claude_ready' });
        } else {
          bubble('Erreur', m.detail || 'installation de claude-code impossible', 'err');
        }
        break;
      case 'session_ended':
        mode.textContent = 'session terminee';
        mode.className = 'pill';
        setBusy(false);
        break;
    }
  });

  function submit() {
    var text = input.value.trim();
    if (!text || busy) return;
    input.value = '';
    bubble('Vous', text, 'user');
    setBusy(true);
    window.orca.postMessage({ command: 'chat', text: text });
  }

  send.onclick = submit;
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
  });

  window.orca.postMessage({ command: 'hello' });
  input.focus();
})();
</script>
"""


# --------------------------------------------------------------------------
# La capability
# --------------------------------------------------------------------------

class OrcaCopilot(orca.script.ScriptPluginCapabilityBase):

    def __init__(self):
        super().__init__()
        self.win = None
        self.bridge = None
        self.session = None
        self._snapshot = {}
        # Etat d'ecriture figé sur le thread UI (hello / _on_chat). Le thread du
        # pont n'a pas de contexte d'audit C++ (current_plugin y est vide), il ne
        # doit donc JAMAIS consulter orca.host.edit en direct.
        self._write_ok = False
        self._pending = {}          # id -> {"event": Event, "approved": bool, "result": dict}
        self._pending_lock = threading.Lock()
        self._counter = 0

    # -- identite ----------------------------------------------------------
    # AUTO = False : capability Chatbot (l'utilisateur mene la discussion).
    # La sous-classe OrcaCopilotAuto (AUTO = True) lance l'analyse automatique
    # des que le panneau s'ouvre : piece + indicateurs -> propositions.
    AUTO = False

    def get_name(self):
        return "Orca Copilot"

    def get_default_config(self):
        return {
            "model": "",
            "printer_context": "Imprimante : Sovol Zero (CoreXY compacte, haute vitesse).",
        }

    # -- cycle de vie ------------------------------------------------------
    def execute(self):
        # Les capabilities sont instanciees une fois par chargement du plugin :
        # un second Run retombe sur la meme instance. Sans ce menage, l'ancien
        # panneau continuerait de poster dans le nouveau.
        self._shutdown()
        # Chat et Auto partagent un seul panneau/pont vivant a la fois.
        for other in list(_LIVE_COPILOTS):
            if other is not self:
                other._shutdown()
        _LIVE_COPILOTS.append(self)

        try:
            self.bridge = BridgeServer(self)
            threading.Thread(target=self.bridge.serve_forever, daemon=True).start()
        except Exception as exc:
            return orca.ExecutionResult.failure("bridge", "Pont local impossible a ouvrir: %s" % exc)

        # Sur un build patche, le chat s'ancre en sidebar a droite du plateau ;
        # sinon on retombe sur la fenetre flottante (OrcaSlicer standard).
        if hasattr(orca.host.ui, "create_side_panel"):
            self.win = orca.host.ui.create_side_panel(
                html=PAGE,
                title="Orca Copilot",
                width=460,
                on_message=self.on_message,
                on_close=self.on_close,
            )
        else:
            self.win = orca.host.ui.create_window(
                html=PAGE,
                title="Orca Copilot",
                width=940,
                height=700,
                on_message=self.on_message,
                on_close=self.on_close,
                style=orca.host.ui.WINDOW_MODELESS,
            )

        # Point de depart apres l'ouverture du panneau : en Auto l'analyse part
        # seule ; en Chat on affiche la suite des instructions. Si claude-code
        # manque, il est installe d'abord (thread), et le depart repasse par la
        # page (aller-retour) pour retomber sur le thread UI.
        if _find_claude_bin() is not None:
            self._on_claude_ready()
        else:
            self.post({"command": "info",
                       "text": "claude-code introuvable : installation en cours "
                               "(installateur officiel, puis npm en repli). "
                               "Le Copilot demarrera des que c'est pret."})
            self.post({"command": "busy",
                       "text": "Installation de claude-code (peut prendre quelques minutes)..."})
            threading.Thread(target=self._install_claude_worker, daemon=True).start()
        return orca.ExecutionResult.success("Orca Copilot ouvert.")

    def _on_claude_ready(self):
        """Thread UI uniquement (execute ou aller-retour par la page)."""
        if self.AUTO:
            # Phase de cadrage : l'utilisateur ordonne ses objectifs AVANT que
            # l'agent ne parte (l'analyse demarre au message auto_start).
            self.post({"command": "auto_setup", "objectives": list(AUTO_OBJECTIVES)})
        else:
            self.post({"command": "info", "text": WELCOME_TEXT})

    def _on_auto_start(self, msg):
        """Thread UI. Declenche l'analyse avec les priorites ordonnees."""
        priorities = msg.get("priorities") or list(AUTO_OBJECTIVES)
        prompt = _auto_prompt_with_priorities(priorities, msg.get("note", ""))
        self.post({"command": "info",
                   "text": "Analyse automatique lancee. Priorites : "
                           + " > ".join(priorities[:3])
                           + (" > ..." if len(priorities) > 3 else "")})
        self.post({"command": "busy", "text": "Analyse automatique en cours..."})
        self._on_chat(prompt)

    def _install_claude_worker(self):
        """Thread : installe claude-code puis rend compte via la page, qui
        renvoie claude_ready pour relancer le demarrage sur le thread UI."""
        ok, detail = _install_claude()
        self.post({"command": "claude_status", "ok": ok, "detail": detail})

    def on_close(self, *_):
        self._shutdown()

    def on_unload(self):
        self._shutdown()

    def _shutdown(self):
        if self in _LIVE_COPILOTS:
            _LIVE_COPILOTS.remove(self)
        # D'abord liberer les confirmations en attente : un thread du pont bloque
        # dans Event.wait() attendrait sinon son timeout de 5 minutes pour rien.
        with self._pending_lock:
            for entry in self._pending.values():
                entry["approved"] = False
                entry["result"] = {"applied": False, "reason": "panneau ferme"}
                entry["event"].set()
            self._pending.clear()

        if self.session is not None:
            try:
                self.session.stop()
            except Exception:
                pass
            self.session = None
        if self.bridge is not None:
            try:
                self.bridge.shutdown()
                self.bridge.server_close()
            except Exception:
                pass
            self.bridge = None
        if self.win is not None:
            try:
                if self.win.is_open():
                    self.win.close()
            except Exception:
                pass
            self.win = None

    # -- sortie vers la page (sur depuis n'importe quel thread) -------------
    def post(self, payload):
        win = self.win
        if win is None:
            return
        try:
            if win.is_open():
                win.post(payload)
        except Exception:
            _log("post a echoue:", traceback.format_exc())

    # -- entree depuis la page : THREAD UI, rester bref ---------------------
    def on_message(self, msg):
        msg = msg or {}
        command = msg.get("command")
        try:
            if command == "hello":
                self._on_hello()
            elif command == "chat":
                self._on_chat(msg.get("text", ""))
            elif command == "apply_response":
                self._on_apply_response(msg)
            elif command == "rotate_response":
                self._on_rotate_response(msg)
            elif command == "claude_ready":
                # Aller-retour par la page : l'installation de claude-code vient
                # de finir sur un thread, ce message nous ramene sur le thread UI.
                self._on_claude_ready()
            elif command == "auto_start":
                # La page a rendu la liste d'objectifs ordonnee par l'utilisateur.
                self._on_auto_start(msg)
        except Exception as exc:
            _log("on_message:", traceback.format_exc())
            self.post({"command": "error", "text": "%s: %s" % (type(exc).__name__, exc)})

    def _on_hello(self):
        has_api = _has_edit_api()
        self._write_ok = _can_write() if has_api else False
        self.post({"command": "ready", "write": self._write_ok, "has_api": has_api})
        if not has_api:
            self.post({"command": "info", "text":
                       "Build OrcaSlicer standard : l'API d'ecriture (orca.host.edit) est absente. "
                       "Le Copilot conseille mais ne peut pas appliquer."})
        elif not self._write_ok:
            self.post({"command": "info", "text":
                       "Permission d'ecriture non accordee. Active \u00ab Allow modifying settings \u00bb "
                       "pour ce plugin dans Fichier > Plugins, puis rouvre le Copilot."})

    def _on_chat(self, text):
        if not text.strip():
            return
        # Thread UI : c'est le seul endroit ou l'on peut lire orca.host en securite.
        # On fige tout maintenant, le reste du travail se fera sans y toucher.
        # (permission comprise : le thread du pont lira le cache, jamais l'API)
        self._write_ok = _can_write() if _has_edit_api() else False
        self._snapshot = {
            "settings": snapshot_settings(),
            "presets": snapshot_presets(),
            "model": snapshot_model(),
            "captured_at": time.time(),
        }

        if self.session is None:
            cfg = self._config()
            try:
                self.session = ClaudeSession(
                    self,
                    self.bridge.port,
                    self.bridge.token,
                    model=cfg.get("model") or None,
                    extra_prompt=cfg.get("printer_context", ""),
                )
            except Exception as exc:
                self.post({"command": "error", "text": "Lancement de claude impossible: %s" % exc})
                self.post({"command": "turn_done"})
                return

        try:
            self.session.send(text)
        except Exception as exc:
            self.post({"command": "error", "text": "Envoi impossible: %s" % exc})
            self.post({"command": "turn_done"})

    def _on_apply_response(self, msg):
        """Thread UI. C'est ici, et seulement ici, qu'on ecrit dans les presets.

        La page renvoie keys=[...] : les lignes decochees de la carte de diff.
        Sans keys (vieux flux), tout le lot est applique.
        """
        req_id = msg.get("id")
        with self._pending_lock:
            entry = self._pending.get(req_id)
        if entry is None:
            return

        if not msg.get("approved"):
            entry["approved"] = False
            entry["result"] = {"applied": False, "reason": "refuse par l'utilisateur"}
            entry["event"].set()
            return

        selected = msg.get("keys")
        changes_in = entry["changes"]
        if isinstance(selected, list):
            wanted = [str(k) for k in selected]
            changes_in = [c for c in changes_in if c["key"] in wanted]
        if not changes_in:
            entry["approved"] = False
            entry["result"] = {"applied": False, "reason": "aucune ligne coch\u00e9e"}
            entry["event"].set()
            return

        try:
            changes = {c["key"]: c["proposed"] for c in changes_in}
            report = orca.host.edit.apply(changes, preset_type=entry.get("preset_type", "print"))
            try:
                orca.host.edit.reslice()
            except Exception:
                pass
            entry["result"] = {"applied": True, "report": report}
        except Exception as exc:
            entry["result"] = {"applied": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            self.post({"command": "error", "text": "Ecriture refusee: %s" % exc})
        entry["approved"] = True
        entry["event"].set()

    def _config(self):
        try:
            raw = self.get_config()
            cfg = json.loads(raw) if raw else {}
        except Exception:
            cfg = {}
        defaults = self.get_default_config()
        for key, value in defaults.items():
            cfg.setdefault(key, value)
        return cfg

    # -- outils MCP : appeles depuis un thread du pont ----------------------
    def handle_tool(self, method, params):
        snap = self._snapshot
        if not snap:
            raise RuntimeError("aucun instantane de configuration - envoie d'abord un message")

        if method == "get_settings":
            return self._tool_get_settings(params, snap)
        if method == "get_setting_metadata":
            keys = params.get("keys") or []
            if not keys:
                raise ValueError("get_setting_metadata attend keys=[...]")
            return setting_metadata(keys[:40])
        if method == "get_presets":
            return snap["presets"]
        if method == "get_model_info":
            return snap["model"]
        if method == "apply_settings":
            return self._tool_apply(params)
        if method == "rotate_objects":
            return self._tool_rotate(params)
        if method == "get_slicing_status":
            return self._tool_slicing_status()
        raise ValueError("outil inconnu: %s" % method)

    def _tool_get_settings(self, params, snap):
        settings = snap["settings"]
        keys = params.get("keys")
        search = (params.get("search") or "").strip().lower()

        if keys:
            out = {k: settings.get(k) for k in keys}
            missing = [k for k, v in out.items() if v is None and k not in settings]
            return {"settings": out, "unknown_keys": missing}

        if search:
            hits = {k: v for k, v in settings.items() if search in k.lower()}
            trimmed = dict(list(hits.items())[:150])
            return {"settings": trimmed, "match_count": len(hits),
                    "truncated": len(hits) > len(trimmed)}

        essential = {k: settings[k] for k in ESSENTIAL_KEYS if k in settings}
        return {
            "settings": essential,
            "note": "Reglages essentiels seulement. %d cles au total : utilise "
                    "keys=[...] ou search=\"...\" pour le reste." % len(settings),
            "total_keys": len(settings),
        }

    def _tool_apply(self, params):
        """Bloque le thread du pont jusqu'a la reponse de l'utilisateur sur la page.

        Ce code tourne sur le thread du pont, hors contexte d'audit C++ : la
        permission se lit dans le cache figé sur le thread UI (_write_ok), jamais
        via orca.host.edit en direct. L'ecriture elle-meme repart vers le thread
        UI (_on_apply_response), ou le binding revalide la permission.
        """
        if not _has_edit_api():
            raise RuntimeError(
                "Ce build d'OrcaSlicer est en lecture seule (orca.host.edit absent). "
                "Presente tes recommandations sous forme de tableau au lieu de les appliquer.")
        if not self._write_ok:
            raise RuntimeError(
                "L'utilisateur n'a pas accorde la permission d'ecriture a ce plugin "
                "(Fichier > Plugins > Allow modifying settings). Presente tes "
                "recommandations sous forme de tableau au lieu de les appliquer.")

        changes = params.get("changes") or []
        if not changes:
            raise ValueError("apply_settings attend changes=[{key, value, reason}]")
        if len(changes) > 25:
            raise ValueError("trop de changements d'un coup (max 25)")

        settings = self._snapshot.get("settings", {})
        normalised = []
        for c in changes:
            key = c.get("key")
            value = c.get("value", c.get("proposed"))
            if not key or value is None:
                raise ValueError("chaque changement demande key et value")
            normalised.append({
                "key": key,
                "proposed": str(value),
                "current": settings.get(key),
                "reason": c.get("reason", ""),
            })

        self._counter += 1
        req_id = "apply-%d" % self._counter
        entry = {
            "event": threading.Event(),
            "approved": False,
            "result": None,
            "changes": normalised,
            "preset_type": params.get("preset_type", "print"),
        }
        with self._pending_lock:
            self._pending[req_id] = entry

        self.post({"command": "apply_request", "id": req_id, "changes": normalised})

        if not entry["event"].wait(timeout=300):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError("l'utilisateur n'a pas repondu au diff en 5 minutes")

        with self._pending_lock:
            self._pending.pop(req_id, None)
        return entry["result"] or {"applied": False, "reason": "sans reponse"}

    def _tool_slicing_status(self):
        """Lecture VIVE (pas d'instantane) : le binding marshale lui-meme vers
        le thread UI, donc l'appel est sur depuis le thread du pont."""
        fn = getattr(orca.host, "slicing_status", None)
        if fn is None:
            raise RuntimeError("ce build n'expose pas orca.host.slicing_status")
        return fn()

    def _tool_rotate(self, params):
        """Bloque le thread du pont jusqu'a la reponse de l'utilisateur sur la
        carte de rotation. Memes regles que _tool_apply : permission figee sur
        le thread UI, ecriture reelle via _on_rotate_response."""
        if not _has_edit_api():
            raise RuntimeError(
                "Ce build d'OrcaSlicer est en lecture seule (orca.host.edit absent). "
                "Explique a l'utilisateur comment orienter la piece dans la vue 3D.")
        if not self._write_ok:
            raise RuntimeError(
                "L'utilisateur n'a pas accorde la permission d'ecriture a ce plugin "
                "(Fichier > Plugins > Allow modifying settings). Explique la rotation "
                "a faire a la main dans la vue 3D.")

        rotation = params.get("rotation") or {}
        try:
            angles = {axis: float(rotation.get(axis, 0.0))
                      for axis in ("x", "y", "z")}
        except (TypeError, ValueError):
            raise ValueError("rotation attend {\"x\": deg, \"y\": deg, \"z\": deg} en degres")
        if not any(angles.values()):
            raise ValueError("rotation doit avoir au moins un axe non nul, ex {\"z\": 90}")

        objects = params.get("objects")
        if objects is not None:
            # Les modeles emettent parfois des flottants ([0.0, 1.0]) : on
            # accepte tout numerique et on normalise en int, sinon l'outil
            # echoue et l'agent se rabat sur un seul objet.
            if not isinstance(objects, list):
                raise ValueError("objects attend une liste d'indices (get_model_info), ou null pour tout le plateau")
            try:
                objects = [int(i) for i in objects]
            except (TypeError, ValueError):
                raise ValueError("objects attend une liste d'indices numeriques, ou null pour tout le plateau")

        drop_to_bed = bool(params.get("drop_to_bed", True))
        if not self._snapshot.get("model", {}).get("objects"):
            raise RuntimeError("le plateau est vide")

        self._counter += 1
        req_id = "rotate-%d" % self._counter
        entry = {
            "event": threading.Event(),
            "approved": False,
            "result": None,
            "kind": "rotate",
            "rotation": angles,
            "objects": objects,
            "drop_to_bed": drop_to_bed,
        }
        with self._pending_lock:
            self._pending[req_id] = entry

        self.post({"command": "rotate_request", "id": req_id,
                   "rotation": angles, "objects": objects,
                   "drop_to_bed": drop_to_bed,
                   "reason": params.get("reason", "")})

        if not entry["event"].wait(timeout=300):
            raise TimeoutError("l'utilisateur n'a pas repondu a la rotation en 5 minutes")

        with self._pending_lock:
            self._pending.pop(req_id, None)
        return entry["result"] or {"applied": False, "reason": "sans reponse"}

    def _on_rotate_response(self, msg):
        """Thread UI. C'est ici, et seulement ici, qu'on touche au modele."""
        req_id = msg.get("id")
        with self._pending_lock:
            entry = self._pending.get(req_id)
        if entry is None or entry.get("kind") != "rotate":
            return

        if not msg.get("approved"):
            entry["approved"] = False
            entry["result"] = {"applied": False, "reason": "refuse par l'utilisateur"}
            entry["event"].set()
            return

        try:
            report = orca.host.edit.rotate_objects(
                rotation=entry["rotation"],
                objects=entry["objects"],
                drop_to_bed=entry["drop_to_bed"])
            entry["result"] = {"applied": True, "report": report}
            # Resultat visible immediatement : si l'agent n'en a vise qu'un,
            # l'ecart saute aux yeux au lieu de passer inapercu.
            rotated = (report or {}).get("rotated") or []
            self.post({"command": "info",
                       "text": "Rotation appliquee : objet(s) %s (%d)."
                               % (", ".join(str(i) for i in rotated) or "aucun", len(rotated))})
        except Exception as exc:
            entry["result"] = {"applied": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            self.post({"command": "error", "text": "Rotation refusee: %s" % exc})
        entry["approved"] = True
        entry["event"].set()


class OrcaCopilotAuto(OrcaCopilot):
    """Capability « Automatique » du bouton AI optimisation : meme panneau,
    meme pont, mais l'analyse (piece + indicateurs -> propositions) part
    seule des l'ouverture."""

    AUTO = True

    def get_name(self):
        return "Orca Copilot Auto"


@orca.plugin
class OrcaCopilotPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(OrcaCopilot)
        orca.register_capability(OrcaCopilotAuto)
        # ColorTest est defini plus bas : register_capabilities() n'est appele
        # qu'apres le chargement complet du module, donc le nom est resolu.
        orca.register_capability(ColorTest)


# --------------------------------------------------------------------------
# Macro : test des 4 couleurs de l'extrudeur 1
#
# Quatre lignes paralleles de 5 cm, une par slot de filament, avec entre chaque
# la sequence de changement definie dans le profil imprimante.
#
# Rien n'est invente : longueur de plateau, temperatures, largeur d'extrusion et
# sequences de demarrage/changement/fin viennent toutes de la configuration
# active. Le G-code personnalise d'un profil est un *modele* (il contient
# {next_extruder}, [layer_z], des conditionnelles) : il est rendu par
# orca.host.render_gcode_template, le PlaceholderParser du slicer lui-meme.
# --------------------------------------------------------------------------

DEFAULT_LINE_LENGTH_MM = 50.0
DEFAULT_LINE_SPACING_MM = 6.0
COLOR_COUNT = 4


def _first_value(raw, fallback):
    """Les options vectorielles arrivent en "1.75,1.75,1.75" : on prend la 1re."""
    if raw is None or raw == "":
        return fallback
    return raw.split(",")[0].strip()


def _nth_value(raw, index, fallback):
    """Valeur du slot `index`, en retombant sur la derniere si le vecteur est court."""
    if raw is None or raw == "":
        return fallback
    parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
    if not parts:
        return fallback
    return parts[min(index, len(parts) - 1)]


def _as_float(text, fallback):
    try:
        return float(str(text).strip().rstrip("%"))
    except (TypeError, ValueError):
        return fallback


def _parse_printable_area(raw):
    """"0x0,256x0,256x256,0x256" -> (xmin, ymin, xmax, ymax)."""
    points = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if "x" not in chunk:
            continue
        try:
            x, y = chunk.split("x", 1)
            points.append((float(x), float(y)))
        except ValueError:
            continue
    if len(points) < 3:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _line_width(settings, nozzle):
    """initial_layer_line_width peut valoir 0 (auto) ou un pourcentage de la buse."""
    raw = _first_value(settings.get("initial_layer_line_width"), "")
    if raw.endswith("%"):
        return nozzle * _as_float(raw, 100.0) / 100.0
    width = _as_float(raw, 0.0)
    if width <= 0:
        return nozzle * 1.125
    return width


def _extrusion_per_mm(width, height, filament_diameter):
    """Section du cordon : rectangle a bouts arrondis, le modele du slicer."""
    section = height * (width - height) + math.pi * (height / 2.0) ** 2
    filament_section = math.pi * (filament_diameter / 2.0) ** 2
    if filament_section <= 0:
        return 0.0
    return section / filament_section


def _render_template(template, variables, notes, label):
    """Rend un modele du profil. En cas d'echec, on le signale au lieu de
    produire du G-code casse silencieusement."""
    if not template or not template.strip():
        return ""
    fn = getattr(orca.host, "render_gcode_template", None)
    if fn is None:
        notes.append("%s : orca.host.render_gcode_template absent de ce build ; "
                     "sequence omise (le G-code du profil n'a pas ete resolu)." % label)
        return ""
    try:
        return fn(template, variables)
    except Exception as exc:
        notes.append("%s : modele non resolu (%s). Sequence remplacee par un "
                     "changement d'outil nu." % (label, exc))
        return None


def build_color_test_gcode(config):
    """Thread UI uniquement : lit orca.host. Renvoie (gcode, notes)."""
    bundle = orca.host.preset_bundle()
    keys = set(bundle.full_config_keys())

    def get(key, default=None):
        return bundle.full_config_value(key) if key in keys else default

    notes = []

    area = _parse_printable_area(get("printable_area"))
    if area is None:
        raise RuntimeError("impossible de lire printable_area dans le profil imprimante")
    xmin, ymin, xmax, ymax = area

    length = float(config.get("line_length_mm", DEFAULT_LINE_LENGTH_MM))
    spacing = float(config.get("line_spacing_mm", DEFAULT_LINE_SPACING_MM))

    bed_w = xmax - xmin
    bed_d = ymax - ymin
    if length > bed_w - 20.0:
        length = max(20.0, bed_w - 20.0)
        notes.append("Longueur ramenee a %.0f mm : le plateau fait %.0f mm de large."
                     % (length, bed_w))
    span = spacing * (COLOR_COUNT - 1)
    if span > bed_d - 20.0:
        spacing = max(2.0, (bed_d - 20.0) / (COLOR_COUNT - 1))
        span = spacing * (COLOR_COUNT - 1)
        notes.append("Espacement ramene a %.1f mm faute de profondeur." % spacing)

    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    x_start = cx - length / 2.0
    x_end = cx + length / 2.0
    y_first = cy - span / 2.0

    nozzle = _as_float(_first_value(get("nozzle_diameter"), "0.4"), 0.4)
    height = _as_float(get("initial_layer_print_height"), 0.2)
    width = _line_width({"initial_layer_line_width": get("initial_layer_line_width")}, nozzle)
    filament_d = _as_float(_first_value(get("filament_diameter"), "1.75"), 1.75)
    e_per_mm = _extrusion_per_mm(width, height, filament_d)

    travel_f = _as_float(get("travel_speed"), 150.0) * 60.0
    print_f = _as_float(_first_value(get("initial_layer_speed"), "20"), 20.0) * 60.0

    relative_e = str(get("use_relative_e_distances", "1")).strip() in ("1", "true", "True")

    bed_temp = _as_float(_nth_value(get("hot_plate_temp_initial_layer"), 0,
                                    _nth_value(get("hot_plate_temp"), 0, "60")), 60.0)

    out = []
    add = out.append
    add("; ==========================================================")
    add("; Orca Copilot - test des %d couleurs de l'extrudeur 1" % COLOR_COUNT)
    add("; genere le %s" % time.strftime("%Y-%m-%d %H:%M"))
    add("; process : %s" % bundle.prints.edited_preset().name)
    add("; machine : %s" % bundle.printers.edited_preset().name)
    add(";")
    add("; A RELIRE AVANT IMPRESSION. Ce fichier est genere a partir de la")
    add("; configuration active, il n'a pas ete tranche ni verifie par le slicer.")
    add("; ==========================================================")
    add("")

    start = _render_template(get("machine_start_gcode"), {
        "initial_extruder": 0, "initial_no_support_extruder": 0,
        "first_layer_temperature": _as_float(_nth_value(get("nozzle_temperature_initial_layer"), 0, "220"), 220.0),
        "first_layer_bed_temperature": bed_temp,
        "bed_temperature": bed_temp, "layer_z": height, "layer_num": 1,
    }, notes, "machine_start_gcode")
    if start:
        add("; --- machine_start_gcode (profil) ---")
        add(start.rstrip())
        add("")
    elif start is None:
        add("; machine_start_gcode du profil non resolu -- demarrage minimal")
        add("G28 ; home")
        add("M140 S%.0f" % bed_temp)
        add("M190 S%.0f" % bed_temp)
        add("")

    add("G90 ; coordonnees absolues")
    add("M83 ; extrusion relative" if relative_e else "M82 ; extrusion absolue")
    if not relative_e:
        add("G92 E0")
    add("")

    e_total = 0.0
    for slot in range(COLOR_COUNT):
        temp = _as_float(_nth_value(get("nozzle_temperature_initial_layer"), slot, "220"), 220.0)
        ftype = _nth_value(get("filament_type"), slot, "?")
        colour = _nth_value((get("filament_colour") or "").replace(";", ","), slot, "?")
        y = y_first + spacing * slot

        add("; ----------------------------------------------------------")
        add("; couleur %d/%d - slot %d - %s - %s" % (slot + 1, COLOR_COUNT, slot + 1, ftype, colour))
        add("; ----------------------------------------------------------")

        if slot == 0:
            add("M109 S%.0f ; chauffe et attend" % temp)
        else:
            change = _render_template(get("change_filament_gcode"), {
                "previous_extruder": slot - 1, "next_extruder": slot,
                "current_filament_id": slot - 1, "next_filament_id": slot,
                "current_hotend": 0, "next_hotend": 0,
                "current_nozzle_id": 0, "next_nozzle_id": 0,
                "layer_num": 1, "layer_z": height, "toolchange_z": height,
                "new_filament_temp": temp, "old_filament_temp": _as_float(
                    _nth_value(get("nozzle_temperature_initial_layer"), slot - 1, "220"), 220.0),
                "flush_length": 0.0, "toolchange_count": slot,
            }, notes, "change_filament_gcode (couleur %d)" % (slot + 1))
            add("M104 S%.0f" % temp)
            if change:
                add("; --- change_filament_gcode (profil) ---")
                add(change.rstrip())
            else:
                add("T%d ; changement d'outil nu" % slot)
            add("M109 S%.0f" % temp)

        add("G1 Z%.3f F600" % (height + 2.0))
        add("G1 X%.3f Y%.3f F%.0f ; approche" % (x_start, y, travel_f))
        add("G1 Z%.3f F600" % height)
        e_line = length * e_per_mm
        if relative_e:
            add("G1 X%.3f Y%.3f E%.4f F%.0f ; ligne de %.0f mm" % (x_end, y, e_line, print_f, length))
        else:
            e_total += e_line
            add("G1 X%.3f Y%.3f E%.4f F%.0f ; ligne de %.0f mm" % (x_end, y, e_total, print_f, length))
        add("G1 Z%.3f F600 ; degage" % (height + 2.0))
        add("")

    end = _render_template(get("machine_end_gcode"), {
        "layer_z": height, "layer_num": 1, "max_layer_z": height,
    }, notes, "machine_end_gcode")
    if end:
        add("; --- machine_end_gcode (profil) ---")
        add(end.rstrip())
    else:
        add("; machine_end_gcode du profil non resolu -- arret minimal")
        add("M104 S0")
        add("M140 S0")
        add("M84")

    # La macro n'ajoute aucune ligne d'amorcage : sur la couleur 1 la buse n'est
    # amorcee que par le machine_start_gcode du profil, et sur les suivantes par
    # la purge du change_filament_gcode. Le dire plutot que d'inventer.
    notes.append("Aucune ligne d'amorcage n'est ajoutee : la couleur 1 depend de "
                 "l'amorcage du machine_start_gcode, les suivantes de la purge du "
                 "change_filament_gcode. Si la premiere ligne sort maigre, c'est la.")

    notes.insert(0, "%d lignes de %.0f mm, espacees de %.1f mm, centrees sur le plateau "
                    "(%.0f x %.0f mm). Cordon %.2f mm de large sur %.2f mm de haut, "
                    "%.3f mm de filament par mm de trajet."
                 % (COLOR_COUNT, length, spacing, bed_w, bed_d, width, height, e_per_mm))
    return "\n".join(out) + "\n", notes


COLOR_TEST_PAGE = r"""
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    background: var(--orca-bg); color: var(--orca-fg);
    font-family: var(--orca-font, system-ui, sans-serif); font-size: 13px;
  }
  header { padding: 12px 14px; border-bottom: 1px solid var(--orca-border); flex: 0 0 auto; }
  header h1 { margin: 0 0 6px; font-size: 13px; font-weight: 600; }
  .notes { color: var(--orca-muted); font-size: 11px; line-height: 1.5; }
  .notes li { margin-bottom: 2px; }
  .warn { color: #d9534f; }
  pre {
    flex: 1 1 auto; overflow: auto; margin: 0; padding: 12px;
    font-family: ui-monospace, monospace; font-size: 11px; line-height: 1.45;
    white-space: pre;
  }
  footer {
    flex: 0 0 auto; border-top: 1px solid var(--orca-border); padding: 10px;
    display: flex; gap: 8px; align-items: center;
  }
  button {
    font: inherit; padding: 6px 14px; border-radius: 7px; cursor: pointer;
    border: 1px solid var(--orca-border); background: transparent; color: var(--orca-fg);
  }
  button.primary { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: transparent; }
  button:disabled { opacity: .45; cursor: default; }
  #status { color: var(--orca-muted); font-size: 11px; }
</style>

<header>
  <h1>Test des 4 couleurs - extrudeur 1</h1>
  <ul class="notes" id="notes"></ul>
</header>

<pre id="gcode">(generation...)</pre>

<footer>
  <button class="primary" id="save">Enregistrer le .gcode...</button>
  <button id="close">Fermer</button>
  <span id="status"></span>
</footer>

<script>
(function () {
  var save = document.getElementById('save');
  var status = document.getElementById('status');

  window.orca.onMessage(function (m) {
    if (!m || !m.command) return;
    if (m.command === 'gcode') {
      document.getElementById('gcode').textContent = m.text;
      var ul = document.getElementById('notes');
      ul.replaceChildren();
      (m.notes || []).forEach(function (n, i) {
        var li = document.createElement('li');
        // La premiere note est le resume des dimensions ; les suivantes sont
        // des avertissements, donc signalees comme tels.
        if (i > 0) li.className = 'warn';
        li.textContent = n;
        ul.appendChild(li);
      });
    } else if (m.command === 'saved') {
      status.textContent = m.path ? ('Enregistre : ' + m.path) : 'Enregistrement annule.';
      save.disabled = false;
    } else if (m.command === 'error') {
      status.textContent = m.text;
      save.disabled = false;
    }
  });

  save.onclick = function () {
    save.disabled = true;
    status.textContent = 'ouverture du dialogue...';
    window.orca.postMessage({ command: 'save' });
  };
  document.getElementById('close').onclick = function () { window.orca.close(); };
  window.orca.postMessage({ command: 'ready' });
})();
</script>
"""


class ColorTest(orca.script.ScriptPluginCapabilityBase):
    """Genere le G-code de test, l'affiche, et n'ecrit sur disque que sur confirmation."""

    def __init__(self):
        super().__init__()
        self.win = None
        self.gcode = ""
        self.notes = []

    def get_name(self):
        return "Test 4 couleurs"

    def get_default_config(self):
        return {"line_length_mm": DEFAULT_LINE_LENGTH_MM,
                "line_spacing_mm": DEFAULT_LINE_SPACING_MM}

    def execute(self):
        if self.win is not None and self.win.is_open():
            self.win.close()
            self.win = None

        try:
            cfg = json.loads(self.get_config() or "{}")
        except Exception:
            cfg = {}
        for key, value in self.get_default_config().items():
            cfg.setdefault(key, value)

        # Thread UI : c'est ici, et seulement ici, qu'on lit orca.host.
        try:
            self.gcode, self.notes = build_color_test_gcode(cfg)
        except Exception as exc:
            _log("generation:", traceback.format_exc())
            return orca.ExecutionResult.failure(
                "gcode", "Generation impossible : %s: %s" % (type(exc).__name__, exc))

        self.win = orca.host.ui.create_window(
            html=COLOR_TEST_PAGE,
            title="Test 4 couleurs",
            width=900,
            height=680,
            on_message=self.on_message,
            on_close=self.on_close,
            style=orca.host.ui.WINDOW_MODELESS,
        )
        return orca.ExecutionResult.success("G-code de test genere (%d lignes)."
                                            % self.gcode.count("\n"))

    def on_close(self, *_):
        self.win = None

    def on_unload(self):
        if self.win is not None:
            try:
                if self.win.is_open():
                    self.win.close()
            except Exception:
                pass
            self.win = None

    def on_message(self, msg):
        msg = msg or {}
        command = msg.get("command")
        try:
            if command == "ready":
                self.win.post({"command": "gcode", "text": self.gcode, "notes": self.notes})
            elif command == "save":
                self._save()
        except Exception as exc:
            _log("on_message:", traceback.format_exc())
            if self.win is not None:
                self.win.post({"command": "error",
                               "text": "%s: %s" % (type(exc).__name__, exc)})

    def _save(self):
        # Un plugin ne peut pas ouvrir un fichier en ecriture : l'audit hook ne lui
        # laisse aucune racine inscriptible sous Linux. C'est l'hote qui ecrit,
        # apres que l'utilisateur a choisi le chemin dans un dialogue natif.
        save_file = getattr(orca.host.ui, "save_file", None)
        if save_file is None:
            self.win.post({"command": "error", "text":
                           "orca.host.ui.save_file absent de ce build : selectionne le "
                           "G-code ci-dessus et copie-le a la main."})
            return
        path = save_file(
            self.gcode,
            suggested_name="test_4_couleurs.gcode",
            title="Enregistrer le test 4 couleurs",
            wildcard="G-code (*.gcode)|*.gcode|Tous les fichiers (*.*)|*.*",
        )
        self.win.post({"command": "saved", "path": path})
