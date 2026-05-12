#!/usr/bin/env python3
"""
Playlist Cover Helper for Apple Music (macOS)

What it can do:
- List user playlists in Music.
- Export the first track's artwork as a suggested playlist cover.
- Copy generated cover to clipboard.
- Reveal playlist in Music for quick manual paste.
- Experimental UI automation paste attempt.

Why this exists:
- Music's scripting bridge allows reading track artwork data.
- It does not reliably expose a direct writable API for playlist cover artwork.
"""

from __future__ import annotations

import re
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple


APP_TITLE = "CoverFix (Legacy)"
OUT_DIR = Path.home() / ".coverfix" / "covers"
OUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Playlist:
    pid: str
    name: str
    track_count: int
    first_has_art: bool


def _run_osascript(script: str) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return False, f"Failed to run osascript: {exc}"

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "unknown AppleScript error"
        return False, err
    return True, proc.stdout.strip()


def _as_quote(s: str) -> str:
    # AppleScript string escaping.
    return s.replace("\\", "\\\\").replace("\"", "\\\"")


def _safe_filename(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[^\w\-. ]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "playlist"
    return s[:120]


def fetch_playlists() -> Tuple[bool, List[Playlist] | str]:
    script = r'''
set oldDelims to AppleScript's text item delimiters
set AppleScript's text item delimiters to tab
tell application "Music"
    set rows to {}
    repeat with p in user playlists
        set pName to name of p
        set pId to persistent ID of p
        set tCount to count of tracks of p
        set hasArt to false
        if tCount > 0 then
            try
                set t to track 1 of p
                set hasArt to ((count of artworks of t) > 0)
            end try
        end if
        set end of rows to (pId & tab & pName & tab & (tCount as text) & tab & (hasArt as text))
    end repeat
    set AppleScript's text item delimiters to linefeed
    set outText to rows as text
end tell
set AppleScript's text item delimiters to oldDelims
return outText
'''
    ok, out = _run_osascript(script)
    if not ok:
        return False, out

    playlists: List[Playlist] = []
    if not out:
        return True, playlists

    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        pid, name, tcount, has_art = parts
        try:
            tc = int(tcount)
        except ValueError:
            tc = 0
        playlists.append(
            Playlist(
                pid=pid,
                name=name,
                track_count=tc,
                first_has_art=has_art.lower() == "true",
            )
        )
    return True, playlists


def export_first_track_artwork(playlist_name: str, output_file: Path) -> Tuple[bool, str]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    p = _as_quote(playlist_name)
    out_path = _as_quote(str(output_file))
    script = f'''
tell application "Music"
    try
        set p to user playlist "{p}"
    on error
        return "ERR|PLAYLIST_NOT_FOUND"
    end try

    if (count of tracks of p) is 0 then
        return "ERR|NO_TRACKS"
    end if

    set t to track 1 of p
    if (count of artworks of t) is 0 then
        return "ERR|NO_ARTWORK"
    end if

    set rawData to data of artwork 1 of t
end tell

set outFile to POSIX file "{out_path}"
set fRef to open for access outFile with write permission
try
    set eof fRef to 0
    write rawData to fRef
    close access fRef
on error errMsg number errNum
    try
        close access fRef
    end try
    return "ERR|WRITE_FAILED|" & errNum & "|" & errMsg
end try

return "OK"
'''
    ok, out = _run_osascript(script)
    if not ok:
        return False, out
    if out.startswith("OK"):
        return True, "OK"
    return False, out


def copy_image_to_clipboard(img_path: Path) -> Tuple[bool, str]:
    p = _as_quote(str(img_path))
    script = f'''
set the clipboard to (read POSIX file "{p}" as picture)
return "OK"
'''
    return _run_osascript(script)


def reveal_playlist(playlist_name: str) -> Tuple[bool, str]:
    p = _as_quote(playlist_name)
    script = f'''
tell application "Music"
    reveal user playlist "{p}"
    activate
end tell
return "OK"
'''
    return _run_osascript(script)


def trigger_get_album_artwork_menu() -> Tuple[bool, str]:
    # Chinese menu path; fallback to English if needed.
    script = r'''
tell application "Music" to activate
delay 0.6
tell application "System Events"
    tell process "Music"
        set frontmost to true
        try
            click menu item "获得专辑插图" of menu 1 of menu item "资料库" of menu 1 of menu bar item "文件" of menu bar 1
            return "OK"
        on error
            click menu item "Get Album Artwork" of menu 1 of menu item "Library" of menu 1 of menu bar item "File" of menu bar 1
            return "OK"
        end try
    end tell
end tell
'''
    return _run_osascript(script)


def experimental_auto_paste(playlist_name: str, img_path: Path) -> Tuple[bool, str]:
    """
    Best-effort flow:
    - copy image to clipboard
    - reveal playlist
    - try selecting playlist label in sidebar
    - cmd+I, then cmd+V
    """
    ok, msg = copy_image_to_clipboard(img_path)
    if not ok:
        return False, f"clipboard failed: {msg}"

    p = _as_quote(playlist_name)
    script = f'''
tell application "Music"
    reveal user playlist "{p}"
    activate
end tell
delay 0.8
tell application "System Events"
    tell process "Music"
        set frontmost to true
        set ecs to entire contents of window 1
        set found to false
        repeat with e in ecs
            try
                if (role of e is "AXStaticText") then
                    if ((name of e) as text) is "{p}" then
                        click e
                        set found to true
                        exit repeat
                    end if
                end if
            end try
        end repeat
        if found is false then
            return "ERR|PLAYLIST_ROW_NOT_FOUND"
        end if
        delay 0.3
        keystroke "i" using command down
        delay 0.6
        keystroke "v" using command down
    end tell
end tell
return "OK"
'''
    return _run_osascript(script)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("920x620")
        self.minsize(860, 540)

        self.playlists: List[Playlist] = []
        self.cover_by_pid: Dict[str, Path] = {}
        self._busy = False
        self.status_var = tk.StringVar(value="Ready. Click 'Refresh Playlists' to load data.")

        self._build_ui()
        # Do not block startup; refresh in background right after window is shown.
        self.after(200, self.refresh_playlists)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=tk.X)

        ttk.Button(top, text="Refresh Playlists", command=self.refresh_playlists).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(top, text="Generate Cover For Selected", command=self.generate_selected).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(top, text="Generate Covers For All", command=self.generate_all).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(top, text="Open Covers Folder", command=self.open_covers_folder).pack(
            side=tk.LEFT
        )

        main = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, pady=(10, 10))

        left = ttk.Frame(main, padding=6)
        main.add(left, weight=3)

        right = ttk.Frame(main, padding=6)
        main.add(right, weight=2)

        ttk.Label(left, text="Playlists").pack(anchor=tk.W)

        cols = ("name", "tracks", "first_art", "cover_file")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="Playlist")
        self.tree.heading("tracks", text="Tracks")
        self.tree.heading("first_art", text="First Track Art")
        self.tree.heading("cover_file", text="Generated Cover")
        self.tree.column("name", width=300)
        self.tree.column("tracks", width=70, anchor=tk.E)
        self.tree.column("first_art", width=120, anchor=tk.CENTER)
        self.tree.column("cover_file", width=320)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sc = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sc.set)
        sc.pack(fill=tk.Y, side=tk.RIGHT)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        ttk.Label(right, text="Selected Playlist Actions").pack(anchor=tk.W)
        ttk.Button(
            right,
            text="Reveal Playlist In Music",
            command=self.reveal_selected_playlist,
        ).pack(fill=tk.X, pady=(6, 4))
        ttk.Button(
            right,
            text="Copy Generated Cover To Clipboard",
            command=self.copy_selected_cover,
        ).pack(fill=tk.X, pady=4)
        ttk.Button(
            right,
            text="Experimental Auto Paste",
            command=self.experimental_selected,
        ).pack(fill=tk.X, pady=4)
        ttk.Separator(right).pack(fill=tk.X, pady=10)
        ttk.Button(
            right,
            text="Trigger Music: Get Album Artwork",
            command=self.trigger_album_artwork,
        ).pack(fill=tk.X, pady=4)

        ttk.Label(
            right,
            text=(
                "Note: Apple Music scripting does not provide a reliable direct API\n"
                "to set playlist cover artwork. This helper generates cover files\n"
                "from the first track and speeds up the manual flow."
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))

        ttk.Label(root, text="Log").pack(anchor=tk.W)
        self.log_text = tk.Text(root, height=10, wrap="word")
        self.log_text.pack(fill=tk.BOTH, expand=False)
        self.log_text.configure(state=tk.DISABLED)
        ttk.Label(root, textvariable=self.status_var).pack(anchor=tk.W, pady=(6, 0))

    def _log(self, msg: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _on_select(self, _event=None) -> None:
        pass

    def refresh_playlists(self) -> None:
        if self._busy:
            self._log("[INFO] Refresh already running, please wait.")
            return
        self._busy = True
        self.status_var.set("Refreshing playlists from Music...")
        self._log("Refreshing playlists from Music...")

        def worker() -> None:
            ok, result = fetch_playlists()
            self.after(0, lambda: self._finish_refresh(ok, result))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_refresh(self, ok: bool, result) -> None:
        self._busy = False
        if not ok:
            self.status_var.set("Refresh failed. Check Music permissions and retry.")
            self._log(f"[ERROR] {result}")
            messagebox.showerror(
                APP_TITLE,
                (
                    "Failed to fetch playlists.\n\n"
                    "Please allow Automation/Accessibility permissions for this app,\n"
                    "then click Refresh again.\n\n"
                    f"Details:\n{result}"
                ),
            )
            return

        self.playlists = sorted(result, key=lambda p: p.name.lower())
        for iid in self.tree.get_children():
            self.tree.delete(iid)

        for p in self.playlists:
            cover = self.cover_by_pid.get(p.pid)
            self.tree.insert(
                "",
                tk.END,
                iid=p.pid,
                values=(
                    p.name,
                    p.track_count,
                    "yes" if p.first_has_art else "no",
                    str(cover) if cover else "",
                ),
            )
        self.status_var.set(f"Loaded {len(self.playlists)} playlists.")
        self._log(f"Loaded {len(self.playlists)} playlists.")

    def _selected_playlists(self) -> List[Playlist]:
        selected_ids = self.tree.selection()
        by_id = {p.pid: p for p in self.playlists}
        return [by_id[s] for s in selected_ids if s in by_id]

    def _generate_for_playlist(self, p: Playlist) -> Tuple[bool, str]:
        out_file = OUT_DIR / f"{_safe_filename(p.name)}__{p.pid}.jpg"
        ok, msg = export_first_track_artwork(p.name, out_file)
        if ok:
            self.cover_by_pid[p.pid] = out_file
            self.tree.set(p.pid, "cover_file", str(out_file))
            return True, str(out_file)
        return False, msg

    def generate_selected(self) -> None:
        items = self._selected_playlists()
        if not items:
            messagebox.showinfo(APP_TITLE, "Select at least one playlist.")
            return
        for p in items:
            ok, msg = self._generate_for_playlist(p)
            if ok:
                self._log(f"[OK] {p.name}: {msg}")
            else:
                self._log(f"[WARN] {p.name}: {msg}")

    def generate_all(self) -> None:
        if not self.playlists:
            return
        for p in self.playlists:
            ok, msg = self._generate_for_playlist(p)
            if ok:
                self._log(f"[OK] {p.name}: {msg}")
            else:
                self._log(f"[WARN] {p.name}: {msg}")

    def _single_selected(self) -> Optional[Playlist]:
        items = self._selected_playlists()
        if len(items) != 1:
            messagebox.showinfo(APP_TITLE, "Select exactly one playlist.")
            return None
        return items[0]

    def reveal_selected_playlist(self) -> None:
        p = self._single_selected()
        if not p:
            return
        ok, msg = reveal_playlist(p.name)
        if ok:
            self._log(f"[OK] Revealed playlist: {p.name}")
        else:
            self._log(f"[ERROR] Reveal failed: {msg}")
            messagebox.showerror(APP_TITLE, msg)

    def copy_selected_cover(self) -> None:
        p = self._single_selected()
        if not p:
            return
        cover = self.cover_by_pid.get(p.pid)
        if not cover or not cover.exists():
            ok, msg = self._generate_for_playlist(p)
            if not ok:
                self._log(f"[ERROR] Could not generate cover: {msg}")
                messagebox.showerror(APP_TITLE, msg)
                return
            cover = self.cover_by_pid.get(p.pid)
        if not cover:
            return
        ok, msg = copy_image_to_clipboard(cover)
        if ok:
            self._log(f"[OK] Copied cover to clipboard: {cover}")
        else:
            self._log(f"[ERROR] Clipboard copy failed: {msg}")
            messagebox.showerror(APP_TITLE, msg)

    def experimental_selected(self) -> None:
        p = self._single_selected()
        if not p:
            return
        cover = self.cover_by_pid.get(p.pid)
        if not cover or not cover.exists():
            ok, msg = self._generate_for_playlist(p)
            if not ok:
                self._log(f"[ERROR] Could not generate cover: {msg}")
                messagebox.showerror(APP_TITLE, msg)
                return
            cover = self.cover_by_pid[p.pid]
        ok, msg = experimental_auto_paste(p.name, cover)
        if ok:
            self._log(f"[OK] Experimental auto paste executed for {p.name}")
            messagebox.showinfo(
                APP_TITLE,
                (
                    "Experimental auto paste sent.\n"
                    "If cover did not change, copy the cover and paste manually."
                ),
            )
        else:
            self._log(f"[WARN] Experimental auto paste failed: {msg}")
            messagebox.showwarning(APP_TITLE, msg)

    def trigger_album_artwork(self) -> None:
        ok, msg = trigger_get_album_artwork_menu()
        if ok:
            self._log("[OK] Triggered Music -> Get Album Artwork")
        else:
            self._log(f"[ERROR] Trigger failed: {msg}")
            messagebox.showerror(
                APP_TITLE,
                (
                    "Could not trigger menu automatically.\n"
                    "Please run manually: 文件 -> 资料库 -> 获得专辑插图\n\n"
                    f"Details: {msg}"
                ),
            )

    def open_covers_folder(self) -> None:
        subprocess.run(["open", str(OUT_DIR)], check=False)


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
