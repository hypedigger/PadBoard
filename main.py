"""
Sound of da Pad
Requires: pygame ttkbootstrap  (pip install pygame ttkbootstrap)
Optional: numpy               (pip install numpy)  — suppression des silences
"""

import ctypes
import itertools
import json
import os
import random
import subprocess
import sys
import time
import threading
from tkinter import filedialog, messagebox
import tkinter as tk

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.dialogs import Querybox

import pygame

try:
    import numpy as np
    import pygame.sndarray as _psa
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
APP_NAME  = "PadBoard"
_DIR      = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE        = os.path.join(_DIR, "controller_sounds_config.json")
WINDOW_CONFIG_FILE = os.path.join(_DIR, "window_state.json")
PLAYLIST_DIR       = os.path.join(_DIR, "playlists")
PLAYLIST_EXT       = ".json"
DEFAULT_GEOMETRY   = "940x720"

DEFAULT_PLAYLIST_LABEL = "\u2014  Configuration par d\u00e9faut  \u2014"

AXIS_THRESHOLD    = 0.5
TRIGGER_THRESHOLD = 0.0
HOLD_TIMEOUT_MS   = 5000   # auto-release (protection joycon drift)
RESCAN_INTERVAL_MS = 5000  # rescan périodique des manettes (branchement à chaud)

INPUT_NAMES = {
    "btn_0":  "A",        "btn_1":  "B",       "btn_2": "X",    "btn_3": "Y",
    "btn_4":  "LB",       "btn_5":  "RB",      "btn_6": "Back", "btn_7": "Start",
    "btn_8":  "L3",       "btn_9":  "R3",      "btn_10": "Guide",
    "hat_up":    "D-Pad Haut",  "hat_down":  "D-Pad Bas",
    "hat_left":  "D-Pad Gauche","hat_right": "D-Pad Droite",
    "lt": "LT", "rt": "RT",
    "ls_up":  "Stick G Haut",  "ls_down":  "Stick G Bas",
    "ls_left":"Stick G Gauche","ls_right": "Stick G Droite",
    "rs_up":  "Stick D Haut",  "rs_down":  "Stick D Bas",
    "rs_left":"Stick D Gauche","rs_right": "Stick D Droite",
}

AXIS_INPUTS = {
    0: ("ls_right","ls_left"), 1: ("ls_down","ls_up"),
    2: ("rs_right","rs_left"), 3: ("rs_down","rs_up"),
    4: ("lt", None),           5: ("rt", None),
}
TRIGGER_AXES     = {4, 5}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg"}

TTKB_THEMES = [
    "darkly","superhero","solar","cyborg","vapor","morph","quartz",
    "cosmo","flatly","litera","minty","pulse","sandstone","united","yeti",
]

# ─────────────────────────────────────────────────────────────────────────────
# Windows DWM / Mica
# ─────────────────────────────────────────────────────────────────────────────

def _dwm_set(hwnd, attr, value):
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, attr, ctypes.byref(ctypes.c_int(value)), ctypes.sizeof(ctypes.c_int))
    except Exception:
        pass

def apply_window_effects(root):
    root.update_idletasks()
    hwnd = root.winfo_id()
    _dwm_set(hwnd, 20, 1)   # dark title bar
    _dwm_set(hwnd, 38, 2)   # Mica

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_dur(seconds: float) -> str:
    if seconds <= 0:
        return "\u2014"
    m = int(seconds) // 60
    s = seconds % 60
    return f"{m}:{int(s):02d}" if m else f"{s:.1f}s"


def _trim_silence(sound: pygame.mixer.Sound) -> pygame.mixer.Sound:
    """Strip leading silence (requires numpy). Threshold: ~0.9 % of max amplitude."""
    if not HAS_NUMPY:
        return sound
    try:
        arr    = _psa.array(sound)
        energy = np.abs(arr).max(axis=1) if arr.ndim > 1 else np.abs(arr)
        hits   = np.where(energy > 300)[0]
        if len(hits) == 0 or hits[0] == 0:
            return sound
        return _psa.make_sound(arr[hits[0]:].copy())
    except Exception:
        return sound


def _scan_audio_files(directory: str) -> list:
    """Tous les fichiers audio du dossier **et de ses sous-dossiers**."""
    found = []
    for root_dir, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in AUDIO_EXTENSIONS:
                found.append(os.path.join(root_dir, fn))
    return found


# ── Playlists ────────────────────────────────────────────────────────────────

_INVALID_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def sanitize_playlist_name(name: str) -> str:
    """Nom de playlist utilisable comme nom de fichier (ou '' si invalide)."""
    cleaned = "".join(" " if ch in _INVALID_NAME_CHARS else ch for ch in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:60]


def ensure_playlist_dir():
    try:
        os.makedirs(PLAYLIST_DIR, exist_ok=True)
    except OSError:
        pass


def playlist_path(name: str) -> str:
    return os.path.join(PLAYLIST_DIR, name + PLAYLIST_EXT)


def list_playlists() -> list:
    """Noms des playlists pr\u00e9sentes dans le sous-dossier 'playlists'."""
    try:
        files = os.listdir(PLAYLIST_DIR)
    except OSError:
        return []
    names = [os.path.splitext(f)[0] for f in files
             if f.lower().endswith(PLAYLIST_EXT)]
    return sorted(names, key=str.lower)


def _move_to_trash(path: str) -> bool:
    try:
        class _OP(ctypes.Structure):
            _fields_ = [("hwnd",ctypes.c_void_p),("wFunc",ctypes.c_uint),
                        ("pFrom",ctypes.c_wchar_p),("pTo",ctypes.c_wchar_p),
                        ("fFlags",ctypes.c_ushort),("fAnyAborted",ctypes.c_bool),
                        ("hNameMaps",ctypes.c_void_p),("lpTitle",ctypes.c_wchar_p)]
        op = _OP()
        op.hwnd  = None; op.wFunc = 3
        op.pFrom = os.path.abspath(path) + "\0"; op.pTo = None
        op.fFlags = 0x0040 | 0x0010 | 0x0004   # FOF_ALLOWUNDO|NOCONFIRM|SILENT
        return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0
    except Exception:
        return False


def _create_start_shortcut() -> bool:
    try:
        script  = os.path.abspath(__file__)
        # pythonw.exe : lance l'appli sans fenêtre console
        py   = sys.executable
        pyw  = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(pyw):
            py = pyw
        lnk_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")
        lnk     = os.path.join(lnk_dir, f"{APP_NAME}.lnk")
        wdir    = os.path.dirname(script)
        icon    = os.path.join(wdir, "icon.ico")
        ps = (
            f"$w=$ws=New-Object -ComObject WScript.Shell;"
            f"$s=$ws.CreateShortcut('{lnk}');"
            f"$s.TargetPath='{py}';"
            f"$s.Arguments='\"{script}\"';"
            f"$s.WorkingDirectory='{wdir}';"
            f"$s.Description='{APP_NAME}';"
        )
        if os.path.exists(icon):
            ps += f"$s.IconLocation='{icon},0';"
        ps += "$s.Save()"
        r = subprocess.run(["powershell","-NoProfile","-Command",ps],
                           capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
# Window-state persistence (geometry + theme + alpha)
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(WINDOW_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

def save_state(state: dict):
    try:
        with open(WINDOW_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Startup playlist picker
# ─────────────────────────────────────────────────────────────────────────────

class StartupPlaylistDialog(ttk.Toplevel):
    """Choix de la playlist \u00e0 ouvrir, affich\u00e9 avant la fen\u00eatre principale."""

    def __init__(self, parent, names: list, state: dict):
        super().__init__(parent)
        self.title(f"Playlist \u2014 {APP_NAME}")
        self.resizable(False, False)
        self.result = None
        self._names = names
        self._state = state
        self.protocol("WM_DELETE_WINDOW", self._skip)

        pad = ttk.Frame(self, padding=24)
        pad.pack(fill=BOTH, expand=YES)

        ttk.Label(pad, text="Quelle playlist ouvrir ?",
                  font=("Segoe UI", 11, "bold")).pack(anchor=W)
        ttk.Label(pad, text="Double-cliquez pour ouvrir directement.",
                  font=("Segoe UI", 8), foreground="#6e7891").pack(
                  anchor=W, pady=(2, 12))

        self._list = tk.Listbox(
            pad, height=min(10, len(names) + 1), width=42,
            activestyle="none", borderwidth=0, highlightthickness=0,
            background="#12141c", foreground="#c9d1d9",
            selectbackground="#1f6feb", selectforeground="#ffffff",
            font=("Segoe UI", 10))
        self._list.pack(fill=BOTH, expand=YES)
        self._list.insert(END, DEFAULT_PLAYLIST_LABEL)
        for n in names:
            self._list.insert(END, n)
        self._list.bind("<Double-Button-1>", lambda _e: self._open())
        self._list.bind("<Return>",          lambda _e: self._open())

        last = state.get("startup_playlist")
        self._list.selection_set(names.index(last) + 1 if last in names else 0)
        self._list.see(self._list.curselection()[0])

        self._ask_var = tk.BooleanVar(
            value=bool(state.get("ask_playlist_on_startup", True)))
        ttk.Checkbutton(pad, text="Demander \u00e0 chaque d\u00e9marrage",
                        variable=self._ask_var,
                        bootstyle="secondary-round-toggle").pack(
                        anchor=W, pady=(14, 16))

        btn = ttk.Frame(pad)
        btn.pack(fill=X)
        ttk.Button(btn, text="Ignorer", command=self._skip,
                   bootstyle="secondary-outline", width=12).pack(side=RIGHT)
        ttk.Button(btn, text="Ouvrir", command=self._open,
                   bootstyle="info", width=12).pack(side=RIGHT, padx=(0, 8))

        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        if pw <= 1:                       # parent masqu\u00e9 : on centre \u00e0 l\u2019\u00e9cran
            px, py = 0, 0
            pw, ph = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("+%d+%d" % (
            px + (pw - self.winfo_width()) // 2,
            py + (ph - self.winfo_height()) // 3))
        self.grab_set()
        self.lift()
        self.focus_force()
        self._list.focus_set()

    def _selected_name(self):
        sel = self._list.curselection()
        if not sel or sel[0] == 0:
            return None
        return self._names[sel[0] - 1]

    def _finish(self, name):
        self.result = name
        self._state["ask_playlist_on_startup"] = self._ask_var.get()
        if name:
            self._state["startup_playlist"] = name
        self.destroy()

    def _open(self):
        self._finish(self._selected_name())

    def _skip(self):
        self._finish(None)


def choose_startup_playlist(root, state: dict):
    """Retourne le nom de la playlist \u00e0 charger au d\u00e9marrage, ou None."""
    ensure_playlist_dir()
    names = list_playlists()
    if not names:
        return None
    if not state.get("ask_playlist_on_startup", True):
        last = state.get("startup_playlist")
        return last if last in names else None
    root.withdraw()
    dlg = StartupPlaylistDialog(root, names, state)
    root.wait_window(dlg)
    root.deiconify()
    return dlg.result


# ─────────────────────────────────────────────────────────────────────────────
# Options dialog
# ─────────────────────────────────────────────────────────────────────────────

class OptionsDialog(ttk.Toplevel):
    def __init__(self, parent, state: dict, on_preview, on_apply, on_cancel):
        super().__init__(parent)
        self.title(f"Options \u2014 {APP_NAME}")
        self.resizable(False, False)
        self.grab_set()
        self._state      = state
        self._on_preview = on_preview
        self._on_apply   = on_apply
        self._on_cancel  = on_cancel
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        pad = ttk.Frame(self, padding=28)
        pad.pack(fill=BOTH, expand=YES)

        # ── Theme ────────────────────────────────────────────────────────────
        ttk.Label(pad, text="Th\u00e8me", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=W, pady=(0, 6))

        self._theme_var = tk.StringVar(value=state.get("theme", "darkly"))
        self._theme_idx = TTKB_THEMES.index(self._theme_var.get()) \
                          if self._theme_var.get() in TTKB_THEMES else 0

        ttk.Button(pad, text="\u2190", width=3, bootstyle="secondary-outline",
                   command=self._prev_theme).grid(row=1, column=0, padx=(0, 4))

        self._combo = ttk.Combobox(pad, textvariable=self._theme_var,
                                   values=TTKB_THEMES, state="readonly", width=20)
        self._combo.grid(row=1, column=1, pady=(0, 0))
        self._combo.bind("<<ComboboxSelected>>", self._on_combo)

        ttk.Button(pad, text="\u2192", width=3, bootstyle="secondary-outline",
                   command=self._next_theme).grid(row=1, column=2, padx=(4, 0),
                                                  pady=(0, 20))

        # ── Transparency ─────────────────────────────────────────────────────
        ttk.Label(pad, text="Transparence", font=("Segoe UI", 10, "bold")).grid(
            row=2, column=0, columnspan=3, sticky=W, pady=(14, 6))
        self._alpha_var = tk.DoubleVar(value=state.get("alpha", 0.95))
        alpha_row = ttk.Frame(pad)
        alpha_row.grid(row=3, column=0, columnspan=3, sticky=EW, pady=(0, 24))
        self._alpha_lbl = ttk.Label(alpha_row,
            text=f"{int(self._alpha_var.get()*100)}%", width=5)
        self._alpha_lbl.pack(side=RIGHT)
        ttk.Scale(alpha_row, from_=0.4, to=1.0, variable=self._alpha_var,
                  orient=HORIZONTAL, command=self._upd_alpha,
                  bootstyle="info", length=240).pack(side=LEFT, fill=X, expand=YES)

        ttk.Separator(pad, orient=HORIZONTAL).grid(
            row=4, column=0, columnspan=3, sticky=EW, pady=(0, 20))

        # ── Playlists ────────────────────────────────────────────────────────
        ttk.Label(pad, text="Playlists", font=("Segoe UI", 10, "bold")).grid(
            row=5, column=0, columnspan=3, sticky=W, pady=(0, 8))
        self._ask_var = tk.BooleanVar(
            value=bool(state.get("ask_playlist_on_startup", True)))
        ttk.Checkbutton(pad,
            text="Demander la playlist au d\u00e9marrage",
            variable=self._ask_var,
            bootstyle="secondary-round-toggle").grid(
            row=6, column=0, columnspan=3, sticky=W, pady=(0, 20))

        # ── Start menu shortcut ───────────────────────────────────────────────
        ttk.Label(pad, text="Menu D\u00e9marrer", font=("Segoe UI", 10, "bold")).grid(
            row=7, column=0, columnspan=3, sticky=W, pady=(0, 8))
        ttk.Button(pad,
            text=f"Cr\u00e9er le raccourci \u00ab {APP_NAME} \u00bb",
            command=self._shortcut, bootstyle="secondary-outline").grid(
            row=8, column=0, columnspan=3, sticky=W, pady=(0, 20))

        # ── numpy hint ───────────────────────────────────────────────────────
        if not HAS_NUMPY:
            ttk.Label(pad,
                text="\u26a0  numpy absent : silences non supprim\u00e9s\n"
                     "   pip install numpy",
                font=("Segoe UI", 8), foreground="#ffaa55",
            ).grid(row=9, column=0, columnspan=3, sticky=W, pady=(0, 16))

        # ── Buttons ──────────────────────────────────────────────────────────
        btn = ttk.Frame(pad)
        btn.grid(row=10, column=0, columnspan=3, sticky=E)
        ttk.Button(btn, text="Annuler", command=self._cancel,
                   bootstyle="secondary-outline", width=12).pack(side=LEFT, padx=(0,8))
        ttk.Button(btn, text="Appliquer", command=self._apply,
                   bootstyle="info", width=12).pack(side=LEFT)

    # ── Theme cycling ─────────────────────────────────────────────────────────

    def _set_theme_idx(self, idx: int):
        self._theme_idx = idx % len(TTKB_THEMES)
        self._theme_var.set(TTKB_THEMES[self._theme_idx])
        self._on_preview(TTKB_THEMES[self._theme_idx], self._alpha_var.get())

    def _prev_theme(self):
        self._set_theme_idx(self._theme_idx - 1)

    def _next_theme(self):
        self._set_theme_idx(self._theme_idx + 1)

    def _on_combo(self, _=None):
        t = self._theme_var.get()
        if t in TTKB_THEMES:
            self._theme_idx = TTKB_THEMES.index(t)
        self._on_preview(t, self._alpha_var.get())

    def _upd_alpha(self, _=None):
        v = self._alpha_var.get()
        self._alpha_lbl.configure(text=f"{int(v*100)}%")
        self._on_preview(self._theme_var.get(), v)

    def _shortcut(self):
        if _create_start_shortcut():
            messagebox.showinfo("Raccourci cr\u00e9\u00e9",
                f"\u00ab {APP_NAME} \u00bb ajout\u00e9 au menu D\u00e9marrer.", parent=self)
        else:
            messagebox.showerror("Erreur",
                "Impossible de cr\u00e9er le raccourci.\n"
                "V\u00e9rifiez vos permissions.", parent=self)

    def _apply(self):
        self._state["theme"] = self._theme_var.get()
        self._state["alpha"] = round(self._alpha_var.get(), 2)
        self._state["ask_playlist_on_startup"] = self._ask_var.get()
        self._on_apply(self._state)
        self.destroy()

    def _cancel(self):
        self._on_cancel()
        self.destroy()

# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────

class ControllerSoundApp:
    def __init__(self, root: ttk.Window, state: dict, playlist: str = None):
        self.root   = root
        self._state = state
        self.current_playlist = playlist or None

        root.title(APP_NAME)
        root.geometry(state.get("geometry", DEFAULT_GEOMETRY))
        root.minsize(700, 520)

        pygame.init()
        pygame.mixer.init()
        pygame.mixer.set_num_channels(32)
        pygame.joystick.init()

        # instance_id -> pygame.joystick.Joystick (toutes partagent le même profil)
        self.joysticks: dict  = {}
        self.mappings: dict   = {}
        self.sounds:   dict   = {}
        self.running          = True
        # (instance_id, axis) -> set d'inputs actifs ; instance_id -> set (hat)
        self.axis_prev_active = {}
        self.hat_prev_active  = {}
        # input_id -> set des manettes qui le maintiennent enfoncé
        self._pressed_by: dict = {}
        self._rescan_after    = None
        self._row_tags: dict  = {}
        self._durations: dict = {}
        self._press_afters: dict = {}   # input_id -> after_id (drift guard)

        self._last_click_ts:  float = 0.0
        self._last_click_row: str   = ""
        self._active_channel        = None

        ensure_playlist_dir()
        self.load_config()
        self.build_ui()
        apply_window_effects(root)
        self.detect_controller()
        self._schedule_rescan()

        self.thread = threading.Thread(target=self.poll_controller, daemon=True)
        self.thread.start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ── Treeview style ────────────────────────────────────────────────────────

    def _apply_styles(self):
        s = self.root.style
        s.configure("Treeview",
            background="#12141c", foreground="#c9d1d9",
            fieldbackground="#12141c", rowheight=30, font=("Segoe UI", 9))
        s.configure("Treeview.Heading",
            background="#0c0e14", foreground="#6e7891",
            font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
            background=[("selected","#1f6feb")],
            foreground=[("selected","#ffffff")])

    # ── Build UI ──────────────────────────────────────────────────────────────

    def build_ui(self):
        self._apply_styles()

        # Accent stripe
        tk.Frame(self.root, height=3, bg="#5b8cff").pack(fill=X, side=TOP)

        # ── Header ──────────────────────────────────────────────────────────
        header = ttk.Frame(self.root, padding=(20, 10, 20, 8))
        header.pack(fill=X)

        # Line 1 : status badge + controller name + refresh button
        status_row = ttk.Frame(header)
        status_row.pack(fill=X)

        ttk.Button(status_row, text="\u21ba  Rafraichir",
                   command=self.detect_controller,
                   bootstyle="info-outline", width=14).pack(side=LEFT, padx=(0, 14))

        self.status_badge = ttk.Label(
            status_row, text="\u25cf  Aucun contr\u00f4leur",
            font=("Segoe UI", 9, "bold"), foreground="#ff5757")
        self.status_badge.pack(side=LEFT)

        self.status_label = ttk.Label(
            status_row, text="",
            font=("Segoe UI", 9), foreground="#6e7891")
        self.status_label.pack(side=LEFT, padx=(12, 0))

        # Line 2 : topmost toggle + options
        ctrl_row = ttk.Frame(header)
        ctrl_row.pack(fill=X, pady=(8, 0))

        self._topmost_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl_row, text="Toujours au premier plan",
                        variable=self._topmost_var,
                        command=lambda: self.root.attributes(
                            "-topmost", self._topmost_var.get()),
                        bootstyle="secondary-round-toggle").pack(side=LEFT)

        ttk.Button(ctrl_row, text="\u2699  Options",
                   command=self.open_options,
                   bootstyle="secondary-outline", width=12).pack(side=RIGHT)

        # Line 3 : playlists
        pl_row = ttk.Frame(header)
        pl_row.pack(fill=X, pady=(10, 0))

        ttk.Label(pl_row, text="Playlist", font=("Segoe UI", 9, "bold"),
                  foreground="#6e7891").pack(side=LEFT, padx=(0, 10))

        self._playlist_var = tk.StringVar(value=DEFAULT_PLAYLIST_LABEL)
        self._playlist_combo = ttk.Combobox(
            pl_row, textvariable=self._playlist_var,
            state="readonly", width=28)
        self._playlist_combo.pack(side=LEFT)
        self._playlist_combo.bind("<<ComboboxSelected>>", self._on_playlist_selected)

        ttk.Button(pl_row, text="\U0001f5d1  Supprimer",
                   command=self.delete_playlist,
                   bootstyle="danger-outline", width=14).pack(side=LEFT, padx=(8, 0))
        ttk.Button(pl_row, text="\u2913  Enregistrer sous\u2026",
                   command=self.save_playlist_as,
                   bootstyle="primary-outline", width=20).pack(side=LEFT, padx=(6, 0))

        self._refresh_playlists()

        ttk.Separator(self.root, orient=HORIZONTAL).pack(fill=X)

        # ── Treeview ────────────────────────────────────────────────────────
        list_frame = ttk.Frame(self.root, padding=(16, 8, 16, 0))
        list_frame.pack(fill=BOTH, expand=YES)

        self.tree = ttk.Treeview(
            list_frame, columns=("input","sound","duration"),
            show="headings", selectmode="browse")
        self.tree.heading("input",    text="  Touche")
        self.tree.heading("sound",    text="Son  \u00b7  cliquer pour assigner")
        self.tree.heading("duration", text="Dur\u00e9e")
        self.tree.column("input",    width=170, anchor=W, stretch=False)
        self.tree.column("sound",    minwidth=280, anchor=W)
        self.tree.column("duration", width=90,  anchor=CENTER, stretch=False)
        self.tree.pack(side=LEFT, fill=BOTH, expand=YES)

        sb = ttk.Scrollbar(list_frame, orient=VERTICAL, command=self.tree.yview)
        sb.pack(side=RIGHT, fill=Y)
        self.tree.configure(yscrollcommand=sb.set)

        self.tree.tag_configure("odd",     background="#12141c")
        self.tree.tag_configure("even",    background="#0c0e14")
        self.tree.tag_configure("pressed", background="#ffd166", foreground="#111111")
        self.tree.tag_configure("odd_missing",  background="#12141c", foreground="#ff8c42")
        self.tree.tag_configure("even_missing", background="#0c0e14", foreground="#ff8c42")

        for iid, name in INPUT_NAMES.items():
            self.tree.insert("", END, iid=iid,
                             values=(name, "\u2014", "\u2014"))
        self._refresh_rows()

        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        ttk.Separator(self.root, orient=HORIZONTAL).pack(fill=X)

        # ── Action buttons ───────────────────────────────────────────────────
        action = ttk.Frame(self.root, padding=(16, 10, 16, 6))
        action.pack(fill=X)

        left = ttk.Frame(action)
        left.pack(side=LEFT)

        for text, cmd, bstyle in [
            ("\u25b6  Tester",      self.test_sound,      "success-outline"),
            ("\u25a0  Stop",        self.stop_all,         "warning-outline"),
            ("\u2715  Retirer",     self.clear_assignment, "warning-outline"),
            ("\ud83d\uddd1  Corbeille", self.delete_to_trash, "danger-outline"),
            ("\u2193  Sauvegarder", self.save_config,      "info-outline"),
        ]:
            ttk.Button(left, text=text, command=cmd,
                       bootstyle=bstyle, width=14).pack(side=LEFT, padx=(0,6))

        ttk.Button(action,
            text="+  Auto-assigner depuis un dossier",
            command=self.auto_assign_from_dir,
            bootstyle="primary-outline", width=32).pack(side=RIGHT)

        # ── Footer ───────────────────────────────────────────────────────────
        footer = ttk.Frame(self.root, padding=(16, 4, 16, 12))
        footer.pack(fill=X)

        hint = ("Appui : coupe le son en cours et joue le nouveau  "
                "\u00b7  Retirer : retire l\u2019assignation  "
                "\u00b7  Corbeille : d\u00e9place le fichier vers la corbeille Windows  "
                "\u00b7  Sauvegarder : \u00e9crit dans la playlist active "
                "(ou la config par d\u00e9faut)")
        if not HAS_NUMPY:
            hint += "  \u00b7  \u26a0 numpy absent : silences non supprim\u00e9s"

        hint_lbl = ttk.Label(footer, text=hint, justify=LEFT,
                             font=("Segoe UI", 8), foreground="#38405a")
        hint_lbl.pack(anchor=W, fill=X)
        footer.bind("<Configure>", lambda e, l=hint_lbl: l.configure(
            wraplength=max(320, e.width - 8)))

        self.log_label = ttk.Label(
            footer, text="En attente d\u2019une action sur le contr\u00f4leur\u2026",
            font=("Segoe UI", 9), foreground="#5b8cff")
        self.log_label.pack(anchor=W, pady=(5, 0))

    # ── Rows ──────────────────────────────────────────────────────────────────

    def _row_values(self, iid: str):
        """(values, tag) \u00e0 afficher pour une touche, selon le mapping courant."""
        base  = "odd" if list(INPUT_NAMES).index(iid) % 2 else "even"
        label = dur = "\u2014"
        tag   = base
        path  = self.mappings.get(iid, {}).get("sound", "")
        if path:
            label = os.path.basename(path)
            dur   = self._durations.get(iid, "\u2014")
            if iid not in self.sounds:
                label += "  \u26a0"          # fichier introuvable
                tag    = f"{base}_missing"
        return (INPUT_NAMES[iid], label, dur), tag

    def _refresh_rows(self):
        for iid in INPUT_NAMES:
            values, tag = self._row_values(iid)
            self._row_tags[iid] = tag
            self.tree.item(iid, values=values, tags=(tag,))

    # ── Options ───────────────────────────────────────────────────────────────

    def open_options(self):
        original_theme = self._state.get("theme", "darkly")
        original_alpha = self._state.get("alpha", 0.95)

        def on_preview(theme, alpha):
            self.root.style.theme_use(theme)
            self.root.attributes("-alpha", alpha)
            self._apply_styles()

        def on_apply(state):
            self._state.update(state)
            on_preview(state["theme"], state["alpha"])

        def on_cancel():
            on_preview(original_theme, original_alpha)

        OptionsDialog(self.root, dict(self._state), on_preview, on_apply, on_cancel)

    # ── Treeview click ────────────────────────────────────────────────────────

    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        col    = self.tree.identify_column(event.x)
        row    = self.tree.identify_row(event.y)
        if region != "cell" or not row:
            return
        now      = time.monotonic()
        is_dbl   = (now - self._last_click_ts < 0.35 and row == self._last_click_row)
        self._last_click_ts  = now
        self._last_click_row = row
        self.tree.selection_set(row)
        if col == "#2" and not is_dbl:
            self.assign_sound()

    def _ui(self, func, *args):
        """root.after() depuis le thread de polling, sans planter a la fermeture."""
        if not self.running:
            return
        try:
            self.root.after(0, func, *args)
        except (RuntimeError, tk.TclError):
            pass

    # ── Controller ────────────────────────────────────────────────────────────

    def detect_controller(self):
        """Rafraichissement manuel : on repart de z\u00e9ro."""
        self._sync_joysticks(force=True)

    def _schedule_rescan(self):
        """Re-scan p\u00e9riodique : branchement / d\u00e9branchement \u00e0 chaud."""
        if not self.running:
            return
        self._rescan_after = self.root.after(
            RESCAN_INTERVAL_MS, self._rescan_tick)

    def _rescan_tick(self):
        self._rescan_after = None
        try:
            self._sync_joysticks()
        except pygame.error:
            pass
        self._schedule_rescan()

    def _sync_joysticks(self, force: bool = False):
        """Aligne self.joysticks sur les manettes r\u00e9ellement branch\u00e9es.

        Toutes les manettes connect\u00e9es partagent le m\u00eame profil de sons.
        """
        if force:
            for js in self.joysticks.values():
                try:
                    js.quit()
                except pygame.error:
                    pass
            self.joysticks.clear()
            self.axis_prev_active.clear()
            self.hat_prev_active.clear()
            self._release_all()
            pygame.joystick.quit()
            pygame.joystick.init()

        elif pygame.joystick.get_count() == len(self.joysticks):
            return                  # rien n\u2019a chang\u00e9 : pas de reconstruction

        present = {}
        for i in range(pygame.joystick.get_count()):
            try:
                js = pygame.joystick.Joystick(i)
                present[js.get_instance_id()] = js
            except pygame.error:
                continue

        for dev in [d for d in self.joysticks if d not in present]:
            self.joysticks.pop(dev, None)
            self._forget_device(dev)
        for dev, js in present.items():
            self.joysticks.setdefault(dev, js)

        self._update_controller_status()

    def _drop_joystick(self, dev):
        """Retire une manette d\u00e9branch\u00e9e (\u00e9v\u00e9nement JOYDEVICEREMOVED)."""
        js = self.joysticks.pop(dev, None)
        if js is None:
            return
        try:
            js.quit()
        except pygame.error:
            pass
        self._forget_device(dev)
        self._update_controller_status()

    def _forget_device(self, dev):
        """Purge l\u2019\u00e9tat d\u2019une manette d\u00e9branch\u00e9e (touches rest\u00e9es enfonc\u00e9es)."""
        for key in [k for k in self.axis_prev_active if k[0] == dev]:
            self.axis_prev_active.pop(key, None)
        self.hat_prev_active.pop(dev, None)
        for iid in list(self._pressed_by):
            holders = self._pressed_by[iid]
            if dev in holders:
                holders.discard(dev)
                if not holders:
                    self._ui(self._release_ui, iid)

    def _release_all(self):
        for iid in list(self._pressed_by):
            self._ui(self._release_ui, iid)
        self._pressed_by.clear()

    def _update_controller_status(self):
        names = [js.get_name() for js in self.joysticks.values()]
        n = len(names)
        if n == 0:
            self.status_badge.configure(
                text="  \u25cf  Aucun contr\u00f4leur", foreground="#ff5757")
            self.status_label.configure(
                text="Recherche automatique toutes les "
                     f"{RESCAN_INTERVAL_MS // 1000} s\u2026")
        elif n == 1:
            self.status_badge.configure(
                text="  \u25cf  Connect\u00e9", foreground="#3ddc84")
            self.status_label.configure(text=names[0])
        else:
            self.status_badge.configure(
                text=f"  \u25cf  {n} contr\u00f4leurs", foreground="#3ddc84")
            self.status_label.configure(
                text="  \u00b7  ".join(names) + "   (m\u00eame profil)")

    def _axis_active(self, axis: int, value: float) -> set:
        mapping = AXIS_INPUTS.get(axis)
        if not mapping:
            return set()
        pos, neg = mapping
        result   = set()
        if axis in TRIGGER_AXES:
            if pos and value > TRIGGER_THRESHOLD: result.add(pos)
        else:
            if pos and value >  AXIS_THRESHOLD: result.add(pos)
            if neg and value < -AXIS_THRESHOLD: result.add(neg)
        return result

    def poll_controller(self):
        clock = pygame.time.Clock()
        while self.running:
            try:
                for ev in pygame.event.get():
                    # hotplug : r\u00e9action imm\u00e9diate, sans attendre le rescan
                    if ev.type == pygame.JOYDEVICEADDED:
                        self._ui(self._sync_joysticks)
                        continue
                    if ev.type == pygame.JOYDEVICEREMOVED:
                        self._ui(self._drop_joystick, ev.instance_id)
                        continue
                    dev = getattr(ev, "instance_id", None)
                    if ev.type == pygame.JOYBUTTONDOWN:
                        iid = f"btn_{ev.button}"
                        if iid in INPUT_NAMES: self._on_pressed(iid, dev)
                    elif ev.type == pygame.JOYBUTTONUP:
                        iid = f"btn_{ev.button}"
                        if iid in INPUT_NAMES: self._on_released(iid, dev)
                    elif ev.type == pygame.JOYAXISMOTION:
                        key  = (dev, ev.axis)
                        prev = self.axis_prev_active.get(key, set())
                        curr = self._axis_active(ev.axis, ev.value)
                        for iid in curr - prev: self._on_pressed(iid, dev)
                        for iid in prev - curr: self._on_released(iid, dev)
                        self.axis_prev_active[key] = curr
                    elif ev.type == pygame.JOYHATMOTION:
                        hx, hy = ev.value
                        curr = set()
                        if hy > 0: curr.add("hat_up")
                        if hy < 0: curr.add("hat_down")
                        if hx < 0: curr.add("hat_left")
                        if hx > 0: curr.add("hat_right")
                        prev = self.hat_prev_active.get(dev, set())
                        for iid in curr - prev: self._on_pressed(iid, dev)
                        for iid in prev - curr: self._on_released(iid, dev)
                        self.hat_prev_active[dev] = curr
                clock.tick(60)
            except pygame.error:
                pass

    # ── Input events (drift-safe) ─────────────────────────────────────────────

    def _on_pressed(self, iid: str, dev=None):
        self._pressed_by.setdefault(iid, set()).add(dev)
        self._ui(self._press_ui, iid)
        self._play_sound(iid)

    def _press_ui(self, iid: str):
        old = self._press_afters.pop(iid, None)
        if old is not None:
            self.root.after_cancel(old)
        self._set_highlight(iid, True)
        self.log_label.configure(
            text=f"Press\u00e9 : {INPUT_NAMES.get(iid, iid)}")
        # schedule auto-release after HOLD_TIMEOUT_MS (drift protection)
        self._press_afters[iid] = self.root.after(
            HOLD_TIMEOUT_MS, self._auto_release, iid)

    def _auto_release(self, iid: str):
        self._press_afters.pop(iid, None)
        self._pressed_by.pop(iid, None)
        self._set_highlight(iid, False)

    def _on_released(self, iid: str, dev=None):
        holders = self._pressed_by.get(iid)
        if holders is not None:
            holders.discard(dev)
            if holders:
                return          # encore maintenu par une autre manette
            self._pressed_by.pop(iid, None)
        self._ui(self._release_ui, iid)

    def _release_ui(self, iid: str):
        old = self._press_afters.pop(iid, None)
        if old is not None:
            self.root.after_cancel(old)
        self._set_highlight(iid, False)

    def _set_highlight(self, iid: str, active: bool):
        if active:
            self.tree.item(iid, tags=("pressed",))
        else:
            # Recompute correct tag (file might have been re-assigned while pressed)
            base   = "odd" if list(INPUT_NAMES).index(iid) % 2 else "even"
            is_mis = (iid in self.mappings and iid not in self.sounds
                      and self.mappings[iid].get("sound",""))
            tag    = f"{base}_missing" if is_mis else base
            self._row_tags[iid] = tag
            self.tree.item(iid, tags=(tag,))

    # ── Playback ──────────────────────────────────────────────────────────────

    def _play_sound(self, iid: str):
        sound = self.sounds.get(iid)
        if not sound:
            return
        if self._active_channel and self._active_channel.get_busy():
            self._active_channel.stop()
        self._active_channel = sound.play()

    def _load_sound(self, path: str) -> pygame.mixer.Sound:
        return _trim_silence(pygame.mixer.Sound(path))

    # ── Actions ───────────────────────────────────────────────────────────────

    def assign_sound(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning(
                "S\u00e9lection", "S\u00e9lectionnez une touche d\u2019abord.")
            return
        path = filedialog.askopenfilename(
            title="Choisir un fichier son",
            filetypes=[("Fichiers audio","*.wav *.mp3 *.ogg"),("Tous","*.*")])
        if not path:
            return
        iid = sel[0]
        try:
            sound = self._load_sound(path)
        except pygame.error as e:
            messagebox.showerror("Erreur", f"Impossible de charger le son : {e}")
            return
        self.mappings[iid] = {"sound": path}
        self.sounds[iid]   = sound
        dur = _fmt_dur(sound.get_length())
        self._durations[iid] = dur
        self.tree.item(iid, values=(INPUT_NAMES[iid], os.path.basename(path), dur))

    def test_sound(self):
        sel = self.tree.selection()
        if not sel: return
        sound = self.sounds.get(sel[0])
        if not sound: return
        if self._active_channel and self._active_channel.get_busy():
            self._active_channel.stop()
        self._active_channel = sound.play(loops=0)

    def stop_all(self):
        pygame.mixer.stop()

    def _clear_row(self, iid: str):
        self.mappings.pop(iid, None)
        self.sounds.pop(iid, None)
        self._durations.pop(iid, None)
        self.tree.item(iid, values=(INPUT_NAMES[iid], "\u2014", "\u2014"))

    def clear_assignment(self):
        sel = self.tree.selection()
        if sel: self._clear_row(sel[0])

    def delete_to_trash(self):
        sel = self.tree.selection()
        if not sel: return
        iid  = sel[0]
        path = self.mappings.get(iid, {}).get("sound", "")
        if not path:
            messagebox.showinfo("Corbeille", "Aucun son assign\u00e9 \u00e0 cette touche.")
            return
        if not os.path.exists(path):
            messagebox.showwarning("Fichier introuvable",
                f"Le fichier n\u2019existe plus :\n{path}\n\n"
                "L\u2019assignation a \u00e9t\u00e9 retir\u00e9e.")
            self._clear_row(iid)
            return
        if _move_to_trash(path):
            self._clear_row(iid)
        else:
            messagebox.showerror("Erreur",
                f"Impossible de d\u00e9placer vers la corbeille :\n{path}")

    def auto_assign_from_dir(self):
        directory = filedialog.askdirectory(
            title="S\u00e9lectionner un dossier de sons (sous-dossiers inclus)")
        if not directory: return

        audio_files = _scan_audio_files(directory)
        if not audio_files:
            messagebox.showwarning("Dossier vide",
                "Aucun fichier audio (.wav .mp3 .ogg) trouv\u00e9 dans ce dossier "
                "ni dans ses sous-dossiers.")
            return

        inputs = list(INPUT_NAMES.keys())
        if len(audio_files) >= len(inputs):
            assigned = random.sample(audio_files, len(inputs))
        else:
            cyc = itertools.cycle(audio_files)
            assigned = [next(cyc) for _ in inputs]

        errors = []
        for iid, path in zip(inputs, assigned):
            try:
                sound = self._load_sound(path)
            except pygame.error as e:
                errors.append(f"{os.path.basename(path)}: {e}"); continue
            self.mappings[iid] = {"sound": path}
            self.sounds[iid]   = sound
            dur = _fmt_dur(sound.get_length())
            self._durations[iid] = dur
            self.tree.item(iid, values=(INPUT_NAMES[iid], os.path.basename(path), dur))

        if errors:
            messagebox.showwarning("Erreurs de chargement",
                "Certains sons n\u2019ont pas pu \u00eatre charg\u00e9s :\n"
                + "\n".join(errors))
        else:
            nu, ni = len(audio_files), len(inputs)
            note   = (" (sons en double)" if nu < ni
                      else " (s\u00e9lection al\u00e9atoire)" if nu > ni else "")
            ndirs  = len({os.path.dirname(p) for p in audio_files})
            scope  = (f" r\u00e9partis dans {ndirs} dossiers" if ndirs > 1 else "")
            messagebox.showinfo("Auto-assign\u00e9",
                f"{nu} son(s) trouv\u00e9(s){scope}, {ni} touches assign\u00e9es{note}")

    # ── Config ────────────────────────────────────────────────────────────────

    def save_config(self):
        """Sauvegarde vers la cible active : playlist courante, ou config par d\u00e9faut."""
        if self.current_playlist:
            if self._write_playlist(self.current_playlist):
                messagebox.showinfo("Sauvegarde",
                    f"Playlist \u00ab\u00a0{self.current_playlist}\u00a0\u00bb sauvegard\u00e9e dans\n"
                    f"{playlist_path(self.current_playlist)}")
            return
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.mappings, f, indent=2)
            messagebox.showinfo("Sauvegarde",
                f"Config sauvegard\u00e9e dans {CONFIG_FILE}")
        except OSError as e:
            messagebox.showerror("Erreur", f"Impossible de sauvegarder : {e}")

    def load_config(self):
        if self.current_playlist:
            path = playlist_path(self.current_playlist)
            if os.path.exists(path):
                self._load_mappings_file(path)
                return
            self.current_playlist = None
        self._load_mappings_file(CONFIG_FILE)

    def _load_mappings_file(self, path: str) -> int:
        """Remplace les mappings courants par ceux du fichier. Retourne le nb de sons OK."""
        try:
            pygame.mixer.stop()
        except pygame.error:
            pass
        self._active_channel = None
        self.mappings   = {}
        self.sounds     = {}
        self._durations = {}

        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(raw, dict):
            return 0
        if isinstance(raw.get("mappings"), dict):
            raw = raw["mappings"]        # format playlist (avec m\u00e9tadonn\u00e9es)

        for key, val in raw.items():
            if isinstance(val, str):
                self.mappings[f"btn_{key}"] = {"sound": val}
            elif isinstance(val, dict) and "sound" in val:
                self.mappings[key] = {"sound": val["sound"]}

        loaded = 0
        for iid, cfg in self.mappings.items():
            spath = cfg.get("sound", "")
            if not spath or not os.path.exists(spath):
                continue        # mapping conserv\u00e9 : le nom reste visible en orange
            try:
                sound = self._load_sound(spath)
            except pygame.error:
                continue
            self.sounds[iid] = sound
            self._durations[iid] = _fmt_dur(sound.get_length())
            loaded += 1
        return loaded

    # ── Playlists ─────────────────────────────────────────────────────────────

    def _refresh_playlists(self, select: str = None):
        names  = list_playlists()
        values = [DEFAULT_PLAYLIST_LABEL] + names
        self._playlist_combo.configure(values=values)
        target = select or self.current_playlist
        self._playlist_var.set(target if target in names
                               else DEFAULT_PLAYLIST_LABEL)
        return names

    def _on_playlist_selected(self, _event=None):
        name = self._playlist_var.get()
        if name == DEFAULT_PLAYLIST_LABEL:
            self.current_playlist = None
            n = self._load_mappings_file(CONFIG_FILE)
            self._refresh_rows()
            self.log_label.configure(
                text=f"Configuration par d\u00e9faut charg\u00e9e \u2014 {n} son(s)")
            return
        self.open_playlist(name)

    def open_playlist(self, name: str):
        path = playlist_path(name)
        if not os.path.exists(path):
            messagebox.showwarning("Playlist introuvable",
                f"Le fichier de la playlist \u00ab\u00a0{name}\u00a0\u00bb n\u2019existe plus.")
            self.current_playlist = None
            self._refresh_playlists()
            return
        n = self._load_mappings_file(path)
        self.current_playlist = name
        self._state["startup_playlist"] = name
        self._refresh_rows()
        self._refresh_playlists()
        self.log_label.configure(
            text=f"Playlist \u00ab\u00a0{name}\u00a0\u00bb charg\u00e9e \u2014 {n} son(s)")

    def _write_playlist(self, name: str) -> bool:
        ensure_playlist_dir()
        data = {
            "name":     name,
            "app":      APP_NAME,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mappings": self.mappings,
        }
        try:
            with open(playlist_path(name), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except OSError as e:
            messagebox.showerror("Erreur",
                f"Impossible d\u2019enregistrer la playlist :\n{e}")
            return False

    def save_playlist_as(self):
        raw = Querybox.get_string(
            prompt="Nom de la playlist :",
            title="Enregistrer la playlist",
            initialvalue=self.current_playlist or "",
            parent=self.root)
        if raw is None:
            return
        name = sanitize_playlist_name(raw)
        if not name:
            messagebox.showwarning("Nom invalide",
                "Donnez un nom qui ne contient pas seulement des caract\u00e8res "
                "interdits (< > : \" / \\ | ? *).")
            return
        if os.path.exists(playlist_path(name)) and name != self.current_playlist:
            if not messagebox.askyesno("Remplacer ?",
                    f"La playlist \u00ab\u00a0{name}\u00a0\u00bb existe d\u00e9j\u00e0.\n\nLa remplacer ?"):
                return
        if not self._write_playlist(name):
            return
        self.current_playlist = name
        self._state["startup_playlist"] = name
        self._refresh_playlists(select=name)
        n = sum(1 for c in self.mappings.values() if c.get("sound"))
        self.log_label.configure(
            text=f"Playlist \u00ab\u00a0{name}\u00a0\u00bb enregistr\u00e9e \u2014 {n} son(s)")

    def delete_playlist(self):
        name = self._playlist_var.get()
        if name == DEFAULT_PLAYLIST_LABEL:
            messagebox.showinfo("Supprimer une playlist",
                "S\u00e9lectionnez d\u2019abord une playlist dans la liste.")
            return
        if not messagebox.askyesno("Supprimer la playlist",
                f"Supprimer la playlist \u00ab\u00a0{name}\u00a0\u00bb ?\n\n"
                "Seul le fichier de playlist est supprim\u00e9 ; "
                "les fichiers audio ne sont pas touch\u00e9s."):
            return
        path = playlist_path(name)
        if os.path.exists(path) and not _move_to_trash(path):
            try:
                os.remove(path)
            except OSError as e:
                messagebox.showerror("Erreur",
                    f"Impossible de supprimer la playlist :\n{e}")
                return
        if self.current_playlist == name:
            self.current_playlist = None
        if self._state.get("startup_playlist") == name:
            self._state.pop("startup_playlist", None)
        self._refresh_playlists()
        self.log_label.configure(
            text=f"Playlist \u00ab\u00a0{name}\u00a0\u00bb supprim\u00e9e")

    def on_close(self):
        self._state["geometry"] = self.root.geometry()
        save_state(self._state)
        self.running = False
        if self._rescan_after is not None:
            try:
                self.root.after_cancel(self._rescan_after)
            except Exception:
                pass
            self._rescan_after = None
        pygame.quit()
        self.root.destroy()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    state    = load_state()
    root     = ttk.Window(themename=state.get("theme", "darkly"))
    root.attributes("-alpha", state.get("alpha", 0.95))
    playlist = choose_startup_playlist(root, state)
    app      = ControllerSoundApp(root, state, playlist)
    root.mainloop()
