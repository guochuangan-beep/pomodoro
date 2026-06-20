#!/usr/bin/env python3
"""Pomodoro Timer - Minimalist desktop productivity timer.
Zero external dependencies. Python 3.x + tkinter.
"""



# ============================================================
# Constants
# ============================================================

WORK_MINUTES = 25
SHORT_BREAK_MINUTES = 5
LONG_BREAK_MINUTES = 15
POMODOROS_BEFORE_LONG_BREAK = 4
TICK_INTERVAL_MS = 200

# Colors — 主色调 #6366f1 (Indigo)
COLOR_BG = '#f5f3ff'
COLOR_FG = '#1e1b4b'
COLOR_ACCENT = '#6366f1'      # Indigo 主色 — 专注
COLOR_BREAK = '#10b981'       # 翠绿 — 休息
COLOR_MUTED = '#a5b4fc'       # 浅紫灰 — 辅助文字
# Color mapping from TimerState (UI concern, kept here for reference)
STATE_COLORS = {
    'idle':    COLOR_MUTED,
    'working': COLOR_ACCENT,
    'break':   COLOR_BREAK,
    'paused':  COLOR_MUTED,
}

# ============================================================
# Timer State Machine
# ============================================================


class TimerState(enum.IntEnum):
    IDLE = 0
    WORKING = 1
    SHORT_BREAK = 2
    LONG_BREAK = 3
    PAUSED = 4


# ============================================================
# Win32 NOTIFYICONDATA  (module-level, defined once)
# ============================================================


class NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIcon notification data structure."""
    _fields_ = [
        ('cbSize',           wintypes.DWORD),
        ('hWnd',             wintypes.HWND),
        ('uID',              wintypes.UINT),
        ('uFlags',           wintypes.UINT),
        ('uCallbackMessage', wintypes.UINT),
        ('hIcon',            wintypes.HICON),
        ('szTip',            wintypes.WCHAR * 128),
        ('dwState',          wintypes.DWORD),
        ('dwStateMask',      wintypes.DWORD),
        ('szInfo',           wintypes.WCHAR * 256),
        ('uTimeoutOrVersion', wintypes.UINT),
        ('szInfoTitle',      wintypes.WCHAR * 64),
        ('dwInfoFlags',      wintypes.DWORD),
        ('guidItem',      ctypes.c_ubyte * 16),  # GUID (16 bytes), unused
        ('hBalloonIcon',     wintypes.HICON),
    ]


# ============================================================
# Pomodoro Engine  (pure logic, no UI)
# ============================================================


class PomodoroEngine:
    """Core timer logic. Uses root.after() for scheduling; single-threaded.

    Callbacks are set as attributes: on_tick, on_state_change, on_session_end.
    """

    def __init__(self, root):
        self.root = root
        self.state = TimerState.IDLE
        self._prior_state = None
        self.remaining_seconds = float(WORK_MINUTES * 60)
        self.pomodoro_count = 0
        self._tick_job = None
        self._start_time = 0.0
        self._total_seconds = 0.0
        self._elapsed_before_pause = 0.0

        # Simple callables instead of observer pattern
        self.on_tick = None
        self.on_state_change = None
        self.on_session_end = None

    # --- properties ---

    @property
    def is_running(self):
        return self.state in (
            TimerState.WORKING,
            TimerState.SHORT_BREAK,
            TimerState.LONG_BREAK,
        )

    @property
    def is_paused(self):
        return self.state == TimerState.PAUSED

    @property
    def is_idle(self):
        return self.state == TimerState.IDLE

    # --- public commands ---

    def start(self):
        """Start a new work session from IDLE, or resume from PAUSED."""
        if self.state == TimerState.IDLE:
            self._begin_session(TimerState.WORKING, WORK_MINUTES * 60)
            return True
        elif self.state == TimerState.PAUSED:
            return self.resume()
        return False

    def pause(self):
        """Pause the current session."""
        if self.is_running:
            self._prior_state = self.state
            self.state = TimerState.PAUSED
            self._elapsed_before_pause = monotonic() - self._start_time
            self._cancel_tick()
            self._fire_state_change()
            return True
        return False

    def resume(self):
        """Resume a paused session."""
        if self.state == TimerState.PAUSED and self._prior_state is not None:
            self.state = self._prior_state
            self._prior_state = None
            self._start_time = monotonic() - self._elapsed_before_pause
            self._fire_state_change()
            self._schedule_tick()
            return True
        return False

    def reset(self):
        """Reset everything back to IDLE."""
        if self.state != TimerState.IDLE:
            self._cancel_tick()
            self.state = TimerState.IDLE
            self._prior_state = None
            self.remaining_seconds = float(WORK_MINUTES * 60)
            self.pomodoro_count = 0
            self._elapsed_before_pause = 0.0
            self._fire_state_change()
            return True
        return False

    def skip(self):
        """Skip the current session. Work → break; break → IDLE."""
        if self.is_running:
            self._cancel_tick()
            self._advance_session()
            self._fire_session_end()
            self._fire_state_change()
            return True
        return False

    # --- helpers ---

    def get_time_display(self):
        total = max(0, int(self.remaining_seconds))
        minutes = total // 60
        seconds = total % 60
        return f"{minutes:02d}:{seconds:02d}"

    def get_session_label(self):
        labels = {
            TimerState.IDLE: "Ready",
            TimerState.WORKING: "Focus",
            TimerState.SHORT_BREAK: "Short Break",
            TimerState.LONG_BREAK: "Long Break",
            TimerState.PAUSED: "Paused",
        }
        return labels.get(self.state, "")

    # --- internal: session lifecycle ---

    def _begin_session(self, state, total_seconds):
        """Start a new session (work or break). Single helper for all types."""
        self.state = state
        self._total_seconds = total_seconds
        self.remaining_seconds = total_seconds
        self._start_time = monotonic()
        self._elapsed_before_pause = 0.0
        self._fire_state_change()
        self._schedule_tick()

    def _advance_session(self):
        """Transition to the next session after work/break ends."""
        if self.state == TimerState.WORKING:
            self.pomodoro_count += 1
            if self.pomodoro_count % POMODOROS_BEFORE_LONG_BREAK == 0:
                self._begin_session(TimerState.LONG_BREAK, LONG_BREAK_MINUTES * 60)
            else:
                self._begin_session(TimerState.SHORT_BREAK, SHORT_BREAK_MINUTES * 60)
        else:
            self._go_idle()

    def _go_idle(self):
        self.state = TimerState.IDLE
        self.remaining_seconds = float(WORK_MINUTES * 60)

    # --- internal: tick loop ---

    def _tick(self):
        if not self.is_running:
            return
        elapsed = monotonic() - self._start_time
        self.remaining_seconds = max(0.0, self._total_seconds - elapsed)
        if self.on_tick:
            self.on_tick(self.remaining_seconds)
        if self.remaining_seconds <= 0:
            self._on_timer_end()
        else:
            self._schedule_tick()

    def _on_timer_end(self):
        self._cancel_tick()
        self._advance_session()
        self._fire_session_end()
        self._fire_state_change()

    def _schedule_tick(self):
        self._cancel_tick()
        self._tick_job = self.root.after(TICK_INTERVAL_MS, self._tick)

    def _cancel_tick(self):
        if self._tick_job is not None:
            self.root.after_cancel(self._tick_job)
            self._tick_job = None

    # --- internal: callback dispatch ---

    def _fire_state_change(self):
        if self.on_state_change:
            self.on_state_change()

    def _fire_session_end(self):
        if self.on_session_end:
            self.on_session_end()


# ============================================================
# System Tray  (Windows only)
# ============================================================


class SystemTray:
    """Windows system tray icon via Win32 Shell_NotifyIcon."""

    WM_TRAY = 0x8000 + 100  # WM_APP + 100

    def __init__(self, root, on_restore, on_quit):
        self.root = root
        self.on_restore = on_restore
        self.on_quit = on_quit
        self._visible = False
        self._initialized = False

        if sys.platform != 'win32':
            return

        try:
            self._init_icon()
            self._hook_wnd_proc()
            self._initialized = True
        except Exception:
            self._initialized = False

    @property
    def available(self):
        return self._initialized

    # --- icon ---

    def _init_icon(self):
        """Load a standard system icon."""
        IDI_APPLICATION = 32512
        self._hicon = ctypes.windll.user32.LoadIconW(None, IDI_APPLICATION)
        if not self._hicon:
            raise OSError("LoadIconW failed")

    # --- window procedure hook ---

    def _hook_wnd_proc(self):
        """Subclass the tkinter window to receive tray messages."""
        self._hwnd = self.root.winfo_id()

        # Determine pointer sizes (64-bit vs 32-bit)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            LRESULT = ctypes.c_int64
            WPARAM_T = ctypes.c_uint64
            LPARAM_T = ctypes.c_int64
        else:
            LRESULT = ctypes.c_int32
            WPARAM_T = ctypes.c_uint32
            LPARAM_T = ctypes.c_int32

        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T
        )

        # Get CallWindowProcW (for chaining to the original procedure)
        CallWindowProcW = ctypes.windll.user32.CallWindowProcW
        CallWindowProcW.argtypes = [
            LRESULT, wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T
        ]
        CallWindowProcW.restype = LRESULT

        # Build the callback – keep a reference to prevent GC
        @WNDPROC
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY:
                if lparam == 0x0203:  # WM_LBUTTONDBLCLK
                    self.root.after(0, self.on_restore)
                elif lparam == 0x0205:  # WM_RBUTTONUP
                    self.root.after(0, self._show_menu)
                return 0
            # Call original window procedure
            return CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

        self._wnd_proc_cb = wnd_proc

        # Replace window procedure
        SetWindowLongPtrW = ctypes.windll.user32.SetWindowLongPtrW
        SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, WNDPROC]
        SetWindowLongPtrW.restype = LRESULT

        GWL_WNDPROC = -4
        self._old_proc = SetWindowLongPtrW(
            wintypes.HWND(self._hwnd), GWL_WNDPROC, wnd_proc
        )
        if not self._old_proc:
            raise OSError("SetWindowLongPtrW failed")

    # --- show / hide ---

    def show(self):
        if not self._initialized or self._visible:
            return
        self._add_icon()
        self._visible = True

    def hide(self):
        if not self._initialized or not self._visible:
            return
        self._del_icon()
        self._visible = False

    def minimize_to_tray(self):
        self.root.withdraw()
        self.show()

    def restore_from_tray(self):
        self.hide()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # --- Win32 NOTIFYICONDATA ---

    def _add_icon(self):
        nid = self._build_nid()
        ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(nid))  # NIM_ADD = 0

    def _del_icon(self):
        nid = self._build_nid()
        ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(nid))  # NIM_DELETE = 2

    def _build_nid(self):
        """Build a NOTIFYICONDATAW structure."""
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = wintypes.HWND(self._hwnd)
        nid.uID = 1
        nid.uFlags = 1 | 2 | 4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = "Pomodoro Timer"
        return nid

    # --- right-click menu ---

    def _show_menu(self):
        """Create and display a popup context menu."""
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))

        menu = ctypes.windll.user32.CreatePopupMenu()

        MF_STRING = 0x00000000
        MF_SEPARATOR = 0x00000800

        ID_SHOW = 1
        ID_QUIT = 2

        ctypes.windll.user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "Show")
        ctypes.windll.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        ctypes.windll.user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")

        TPM_RETURNCMD = 0x0100
        TPM_LEFTALIGN = 0x0000
        cmd = ctypes.windll.user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_LEFTALIGN, pt.x, pt.y, 0,
            wintypes.HWND(self._hwnd), None,
        )

        ctypes.windll.user32.DestroyMenu(menu)

        if cmd == ID_SHOW:
            self.on_restore()
        elif cmd == ID_QUIT:
            self.on_quit()

    def cleanup(self):
        if self._visible:
            self._del_icon()
            self._visible = False


# ============================================================
# Pomodoro UI
# ============================================================


class PomodoroUI:
    """Tkinter user interface for the Pomodoro timer."""

    def __init__(self):
        self.root = tk.Tk()
        self.engine = PomodoroEngine(self.root)
        self._style = None       # set in _setup_styles()
        self._setup_window()
        self._setup_styles()
        self._build_ui()
        self._bind_events()
        self._bind_engine()

        # System tray (optional)
        self.tray = SystemTray(
            self.root,
            on_restore=self._on_tray_restore,
            on_quit=self._quit_app,
        )

        # Set initial display
        self._update_display()

    # --- window ---

    def _setup_window(self):
        self.root.title("Pomodoro")
        self.root.geometry("300x300")
        self.root.minsize(280, 280)
        self.root.configure(bg=COLOR_BG)

        # Center on screen
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"+{x}+{y}")

        # Close → minimize to tray (if available)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        # High-DPI awareness
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    # --- styles ---

    def _setup_styles(self):
        self._style = ttk.Style()
        # Use 'clam' for consistent cross-platform look
        if 'clam' in self._style.theme_names():
            self._style.theme_use('clam')

        self._style.configure('.', font=('Segoe UI', 10))
        self._style.configure('TFrame', background=COLOR_BG)
        self._style.configure('TLabel', background=COLOR_BG, foreground=COLOR_FG)

        self._style.configure(
            'Timer.TLabel',
            font=('Consolas', 48, 'bold'),
            foreground=COLOR_ACCENT,
            background=COLOR_BG,
            anchor='center',
        )
        self._style.configure(
            'Session.TLabel',
            font=('Segoe UI', 12),
            foreground=COLOR_MUTED,
            background=COLOR_BG,
            anchor='center',
        )
        self._style.configure(
            'Pomodoro.TLabel',
            font=('Segoe UI', 9),
            foreground=COLOR_ACCENT,
            background=COLOR_BG,
            anchor='center',
        )

        # Indigo-themed buttons
        self._style.configure(
            'Control.TButton',
            font=('Segoe UI', 11),
            padding=(12, 6),
            background=COLOR_ACCENT,
            foreground='#ffffff',
        )
        self._style.map('Control.TButton',
            background=[('active', '#4f46e5'), ('pressed', '#4338ca'), ('disabled', '#c7d2fe')],
            foreground=[('disabled', '#e0e7ff')],
        )

        # Checkbutton
        self._style.configure(
            'Topmost.TCheckbutton',
            background=COLOR_BG,
            foreground=COLOR_MUTED,
            font=('Segoe UI', 9),
        )
        self._style.map('Topmost.TCheckbutton',
            background=[('active', COLOR_BG)],
            foreground=[('active', COLOR_ACCENT)],
        )

    # --- layout ---

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=20)
        main.pack(fill='both', expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)

        # Session label
        self.session_label = ttk.Label(
            main, text="Ready", style='Session.TLabel'
        )
        self.session_label.grid(row=0, column=0, columnspan=2, pady=(0, 8))

        # Time display
        self.time_label = ttk.Label(
            main, text="25:00", style='Timer.TLabel'
        )
        self.time_label.grid(row=1, column=0, columnspan=2, pady=(0, 4))

        # Pomodoro counter
        self.pomo_label = ttk.Label(
            main, text="", style='Pomodoro.TLabel'
        )
        self.pomo_label.grid(row=2, column=0, columnspan=2, pady=(0, 16))

        # Buttons — row 3 & 4
        self.start_btn = ttk.Button(
            main, text="Start", style='Control.TButton',
            command=self._on_start,
        )
        self.pause_btn = ttk.Button(
            main, text="Pause", style='Control.TButton',
            command=self._on_pause,
        )
        self.skip_btn = ttk.Button(
            main, text="Skip", style='Control.TButton',
            command=self._on_skip,
        )
        self.reset_btn = ttk.Button(
            main, text="Reset", style='Control.TButton',
            command=self._on_reset,
        )

        self.start_btn.grid(row=3, column=0, padx=(0, 4), pady=4, sticky='ew')
        self.pause_btn.grid(row=3, column=1, padx=(4, 0), pady=4, sticky='ew')
        self.skip_btn.grid(row=4, column=0, padx=(0, 4), pady=4, sticky='ew')
        self.reset_btn.grid(row=4, column=1, padx=(4, 0), pady=4, sticky='ew')

        # Separator
        sep = ttk.Separator(main, orient='horizontal')
        sep.grid(row=5, column=0, columnspan=2, pady=(12, 8), sticky='ew')

        # Always-on-top checkbox
        self.topmost_var = tk.BooleanVar(value=False)
        self.topmost_cb = ttk.Checkbutton(
            main, text="Always on Top", variable=self.topmost_var,
            command=self._toggle_topmost, style='Topmost.TCheckbutton',
        )
        self.topmost_cb.grid(row=6, column=0, columnspan=2)

    # --- event bindings ---

    def _bind_events(self):
        # Keyboard shortcuts (only when window has focus)
        self.root.bind('<space>', lambda e: self._on_start())
        self.root.bind('<KeyPress-r>', lambda e: self._on_reset())
        self.root.bind('<KeyPress-s>', lambda e: self._on_skip())

    def _bind_engine(self):
        self.engine.on_tick = self._on_tick
        self.engine.on_state_change = self._on_state_change
        self.engine.on_session_end = self._on_session_end

    # --- button callbacks ---

    def _on_start(self):
        self.engine.start()

    def _on_pause(self):
        self.engine.pause()

    def _on_skip(self):
        self.engine.skip()

    def _on_reset(self):
        self.engine.reset()

    # --- engine callbacks ---

    def _on_tick(self, _remaining_seconds):
        self._update_time_display()

    def _on_state_change(self):
        self._update_display()
        self._update_buttons()

    def _on_session_end(self):
        self._flash_display()
        self._beep()

    # --- display update ---

    def _update_display(self):
        """Full display refresh (on state change)."""
        self._update_time_display()
        accent = self._get_accent_color()
        self._style.configure('Timer.TLabel', foreground=accent)
        self.session_label.config(text=self.engine.get_session_label())

        count = self.engine.pomodoro_count
        if count > 0:
            self.pomo_label.config(text='● ' * count)
        else:
            self.pomo_label.config(text="")

    def _update_time_display(self):
        """Time-only update (on each tick, 200ms). Avoids unnecessary style reconfig."""
        self.time_label.config(text=self.engine.get_time_display())

    def _get_accent_color(self):
        """Map engine state to display color. UI concern, not engine logic."""
        state = self.engine.state
        if state in (TimerState.SHORT_BREAK, TimerState.LONG_BREAK):
            return COLOR_BREAK
        if state == TimerState.PAUSED:
            return COLOR_MUTED
        return COLOR_ACCENT

    def _update_buttons(self):
        """Enable/disable buttons based on engine state."""
        e = self.engine
        if e.is_idle:
            self.start_btn.config(text="Start", state='normal')
            self.pause_btn.config(state='disabled')
            self.skip_btn.config(state='disabled')
            self.reset_btn.config(state='disabled')
        elif e.is_paused:
            self.start_btn.config(text="Resume", state='normal')
            self.pause_btn.config(state='disabled')
            self.skip_btn.config(state='disabled')
            self.reset_btn.config(state='normal')
        elif e.is_running:
            self.start_btn.config(text="Start", state='disabled')
            self.pause_btn.config(state='normal')
            self.skip_btn.config(state='normal')
            self.reset_btn.config(state='normal')

    # --- visual & audio feedback ---

    def _flash_display(self):
        """Briefly flash the timer text to signal session end."""
        accent = self._get_accent_color()
        flash_colors = ('#ffffff', accent) * 3  # 6 flashes
        step = 0

        def _step():
            nonlocal step
            if step < len(flash_colors):
                self._style.configure('Timer.TLabel', foreground=flash_colors[step])
                step += 1
                self.root.after(150, _step)
            else:
                self._style.configure('Timer.TLabel', foreground=accent)

        _step()

    def _beep(self):
        """Play system notification sound."""
        try:
            ctypes.windll.user32.MessageBeepW(0x40)  # MB_ICONINFORMATION
        except Exception:
            pass

    # --- always-on-top ---

    def _toggle_topmost(self):
        self.root.wm_attributes('-topmost', self.topmost_var.get())

    # --- close / tray behaviour ---

    def _on_close(self):
        if self.tray.available:
            self.tray.minimize_to_tray()
        else:
            self._quit_app()

    def _on_tray_restore(self):
        self.tray.restore_from_tray()

    def _quit_app(self):
        self.tray.cleanup()
        self.root.destroy()

    # --- run ---

    def run(self):
        self.root.mainloop()


# ============================================================
# Entry Point
# ============================================================


def main():
    app = PomodoroUI()
    app.run()


if __name__ == '__main__':
    main()
