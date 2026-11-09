"""WoW: Forever addon manager.

Tiny local tool: installs/updates CurseForge addons into the Forever AddOns folder
and keeps a JSON register of what is installed. No server, no listening sockets;
it only makes outbound HTTPS requests to CurseForge hosts, and exits when closed.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import queue
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

APP_DIR = Path(__file__).resolve().parent
REGISTER_PATH = APP_DIR / "addons.json"
ADDONS_SUBPATH = Path("_forever_") / "Interface" / "AddOns"
COMMON_WOW_DIRS = [r"C:\Program Files (x86)\World of Warcraft", r"C:\Program Files\World of Warcraft",
                   r"D:\World of Warcraft", r"D:\Games\World of Warcraft", r"E:\World of Warcraft"]

API = "https://www.curseforge.com/api/v1/mods"
ALLOWED_HOSTS = {"www.curseforge.com", "edge.forgecdn.net", "mediafilez.forgecdn.net"}
USER_AGENT = "wow-forever-addon-manager/1.0 (+https://github.com/leagueherald/wow-forever-addon-manager)"
RELEASE = 1  # CurseForge releaseType: 1 = release, 2 = beta, 3 = alpha


# ---------------------------------------------------------------- network ---

class NetError(Exception):
    pass


def check_url(url):
    """Only HTTPS to CurseForge hosts is ever allowed."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise NetError(f"Blocked request to non-CurseForge URL: {url}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # every redirect hop must also be allowlisted
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def http_open(url, timeout=30):
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        return _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise NetError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).hostname}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, NetError):
            raise e.reason
        raise NetError(f"Network error: {e.reason}") from e


def get_json(url):
    with http_open(url) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url, dest, expected_size=None):
    size = 0
    with http_open(url, timeout=60) as r, open(dest, "wb") as f:
        while chunk := r.read(65536):
            f.write(chunk)
            size += len(chunk)
    if expected_size and size != expected_size:
        raise NetError(f"Download size mismatch ({size} bytes, expected {expected_size})")


# ------------------------------------------------------------ game version ---

def game_version_prefix(addons_dir):
    """'1.60' for client 1.60.1.x, read from the install's .build.info."""
    flavor_dir = Path(addons_dir).parents[1]          # ...\_forever_
    build_info = flavor_dir.parent / ".build.info"    # ...\World of Warcraft\.build.info
    product = None
    try:
        lines = (flavor_dir / ".flavor.info").read_text(encoding="utf-8").split()
        product = lines[-1].strip()
    except OSError:
        pass
    try:
        rows = build_info.read_text(encoding="utf-8").splitlines()
        cols = [c.split("!")[0] for c in rows[0].split("|")]
        for row in rows[1:]:
            rec = dict(zip(cols, row.split("|")))
            if product is None or rec.get("Product") == product:
                parts = rec.get("Version", "").split(".")
                if len(parts) >= 2:
                    return f"{parts[0]}.{parts[1]}"
    except (OSError, IndexError):
        pass
    return "1.60"


def is_compatible(file_meta, prefix):
    return any(v == prefix or v.startswith(prefix + ".") for v in file_meta.get("gameVersions", []))


# --------------------------------------------------------------- curseforge ---

def get_file(project_id, file_id):
    data = get_json(f"{API}/{project_id}/files/{file_id}").get("data")
    if not data or data.get("projectId") != project_id:
        raise NetError(f"File {file_id} not found in project {project_id} - check the Project ID.")
    return data


def latest_file(project_id, prefix, pages=3):
    """Newest release file compatible with this client, or None."""
    seen_any = False
    for page in range(pages):
        files = get_json(f"{API}/{project_id}/files?pageIndex={page}&pageSize=50"
                         "&sort=dateCreated&sortDescending=true&removeAlphas=true").get("data") or []
        seen_any = seen_any or bool(files)
        for f in files:
            if f.get("releaseType") == RELEASE and is_compatible(f, prefix):
                return f
        if len(files) < 50:
            break
    if not seen_any:
        raise NetError(f"No files found for project {project_id} - check the Project ID.")
    return None


LINK_RE = re.compile(r"curseforge\.com/wow/addons/([A-Za-z0-9_\-]+)(?:/(?:download|files)/(\d+))?", re.I)


def parse_link(text):
    """-> (slug, file_id or None). Raises ValueError if it isn't a CurseForge WoW addon link."""
    m = LINK_RE.search(text.strip())
    if not m:
        raise ValueError("Not a CurseForge WoW addon link (expected curseforge.com/wow/addons/<name>...)")
    return m.group(1).lower(), (int(m.group(2)) if m.group(2) else None)


# ---------------------------------------------------------------- register ---

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_register(path=None):
    try:
        reg = json.loads(Path(path or REGISTER_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reg = {}
    if not reg.get("addons_dir"):
        reg["addons_dir"] = find_addons_dir() or ""
    reg.setdefault("addons", {})
    return reg


def find_addons_dir():
    """The Forever AddOns folder in a common install location, or None."""
    for wow in COMMON_WOW_DIRS:
        d = Path(wow) / ADDONS_SUBPATH
        if d.is_dir():
            return str(d)
    return None


def save_register(reg, path=None):
    path = Path(path or REGISTER_PATH)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------- toc ---

COLOR_RE = re.compile(r"\|c[0-9A-Fa-f]{8}|\|r")


def read_toc(folder):
    """Merged '## Key: value' fields from a folder's .toc files."""
    fields = {}
    for toc in sorted(Path(folder).glob("*.toc")):
        try:
            text = toc.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("##") and ":" in line:
                k, v = line[2:].split(":", 1)
                k, v = k.strip(), v.strip()
                if v and k not in fields and not ("not supported" in v and k == "Title"):
                    fields[k] = v
    if "Title" in fields:
        title = COLOR_RE.sub("", fields["Title"]).strip()
        fields["Title"] = re.sub(r"\s+v?\d+(\.\d+)+\S*$", "", title) or title  # "Questie v12.6.4" -> "Questie"
    return fields


def toc_project_id(fields):
    v = fields.get("X-Curse-Project-ID", "")
    return int(v) if v.isdigit() else None


# -------------------------------------------------------------------- scan ---

def scan(reg):
    """Sync the register with the AddOns folder. Returns a report dict."""
    root = Path(reg["addons_dir"])
    report = {"removed": [], "incomplete": [], "adopted": [], "untracked": []}
    if not root.is_dir():
        raise FileNotFoundError(f"AddOns folder not found: {root}")

    for pid, e in list(reg["addons"].items()):
        present = [f for f in e["folders"] if (root / f).is_dir()]
        if not present:
            del reg["addons"][pid]
            report["removed"].append(e["name"])
        elif len(present) < len(e["folders"]):
            e["missing"] = sorted(set(e["folders"]) - set(present))
            report["incomplete"].append(e["name"])
        else:
            e.pop("missing", None)

    tracked = {f for e in reg["addons"].values() for f in e["folders"]}
    adopt = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in tracked):
        fields = read_toc(d)
        pid = toc_project_id(fields)
        if pid is None:
            report["untracked"].append(d.name)
        else:
            adopt.setdefault(str(pid), []).append((d.name, fields))

    for pid, items in adopt.items():
        if pid in reg["addons"]:  # extra folder for an addon we already track
            reg["addons"][pid]["folders"] = sorted(set(reg["addons"][pid]["folders"]) | {n for n, _ in items})
            continue
        main_name, main = min(items, key=lambda it: len(it[0]))  # "DBM-Core" over "DBM-Core-Extras"
        reg["addons"][pid] = {
            "name": main.get("Title") or main_name,
            "slug": None,
            "project_id": int(pid),
            "file_id": None,
            "version": main.get("Version", "?"),
            "folders": sorted(n for n, _ in items),
            "installed_at": now(),
            "adopted": True,
        }
        report["adopted"].append(reg["addons"][pid]["name"])
    return report


# ------------------------------------------------------------------ updates ---

def _norm(v):
    return re.sub(r"^v", "", (v or "").strip().lower())


def update_due(e):
    latest = e.get("latest")
    if not latest:
        return False
    if e.get("missing"):
        return True
    if e.get("file_id"):
        return latest["file_id"] != e["file_id"]
    # adopted from .toc: no file id yet, compare versions loosely
    v = _norm(e.get("version"))
    return not v or v not in (_norm(latest["version"]) + " " + latest.get("file_name", "").lower())


def check_updates(reg, prefix):
    """Fill e['latest'] for every addon (parallel). Returns names of addons with errors."""
    errors = []

    def one(e):
        try:
            f = latest_file(e["project_id"], prefix)
            e.pop("latest_error", None)
            if f is None:
                e["latest_error"] = "no Forever release"
                return
            e["latest"] = {"file_id": f["id"], "version": f["displayName"],
                           "file_name": f["fileName"], "checked_at": now()}
        except Exception as ex:  # keep checking the others
            e["latest_error"] = str(ex)
            errors.append(e["name"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, list(reg["addons"].values())))
    return errors


# ------------------------------------------------------------------ install ---

def _rmtree(path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def safe_top_folders(zf):
    """Validate zip entries; return the set of top-level folder names."""
    tops = set()
    for name in zf.namelist():
        p = PurePosixPath(name.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or ":" in name or not p.parts:
            raise ValueError(f"Unsafe path in zip: {name}")
        if len(p.parts) == 1 and not name.endswith("/"):
            raise ValueError(f"Unexpected file at zip root: {name}")
        tops.add(p.parts[0])
    if not tops:
        raise ValueError("Zip is empty")
    return tops


def install_file(reg, project_id, meta, slug=None):
    """Download `meta` (a CurseForge file record), replace the addon's folders, update the register.
    Returns the register entry."""
    root = Path(reg["addons_dir"])
    pid = str(project_id)
    old = reg["addons"].get(pid, {})
    tmp = Path(tempfile.mkdtemp(prefix="wowam_"))
    try:
        zpath = tmp / "addon.zip"
        download(f"{API}/{project_id}/files/{meta['id']}/download", zpath, meta.get("fileLength"))
        with zipfile.ZipFile(zpath) as zf:
            if zf.testzip() is not None:
                raise ValueError("Downloaded zip is corrupt")
            tops = safe_top_folders(zf)
            zf.extractall(tmp / "x")
        for name in set(old.get("folders", [])) | tops:
            dest = root / name
            if dest.parent == root and dest.is_dir():
                _rmtree(dest)
        for name in tops:
            shutil.move(str(tmp / "x" / name), str(root / name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    folders = sorted(tops)
    tocs = [(n, read_toc(root / n)) for n in folders]
    main = next((it for it in tocs if toc_project_id(it[1]) == project_id), min(tocs, key=lambda it: len(it[0])))
    slug = slug or old.get("slug")
    pretty = slug.replace("-", " ").title() if slug else None  # "deadly-boss-mods" -> "Deadly Boss Mods"
    entry = {
        "name": pretty or old.get("name") or main[1].get("Title") or main[0],
        "slug": slug,
        "project_id": project_id,
        "file_id": meta["id"],
        "version": meta["displayName"],
        "file_name": meta["fileName"],
        "folders": folders,
        "installed_at": now(),
        "latest": {"file_id": meta["id"], "version": meta["displayName"],
                   "file_name": meta["fileName"], "checked_at": now()},
    }
    if old.get("latest") and old["latest"]["file_id"] != meta["id"]:
        entry["latest"] = old["latest"]  # installed a specific older file on purpose
    reg["addons"][pid] = entry
    save_register(reg)
    return entry


def uninstall(reg, project_id):
    root = Path(reg["addons_dir"])
    e = reg["addons"].pop(str(project_id))
    for name in e["folders"]:
        dest = root / name
        if dest.parent == root and dest.is_dir():
            _rmtree(dest)
    save_register(reg)
    return e


def reveal(path):
    subprocess.Popen(f'explorer /select,"{path}"')


# ======================================================================= UI ===

def run_ui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    reg = load_register()
    root = tk.Tk()

    if not Path(reg["addons_dir"]).is_dir():  # first run, or the game moved: ask once and remember
        messagebox.showinfo("WoW Forever Addons", "Select your WoW Forever AddOns folder, e.g.\n"
                            r"...\World of Warcraft\_forever_\Interface\AddOns")
        chosen = filedialog.askdirectory(title="WoW Forever AddOns folder", mustexist=True)
        if not chosen:
            root.destroy()
            return
        reg["addons_dir"] = str(Path(chosen))
        save_register(reg)

    try:
        prefix = game_version_prefix(reg["addons_dir"])
    except IndexError:
        prefix = "1.60"
    results = queue.Queue()
    state = {"busy": False}

    root.title("WoW Forever Addons")
    root.geometry("700x460")
    root.minsize(560, 300)

    class Tip:
        def __init__(self, widget, text):
            self.w, self.text, self.top = widget, text, None
            widget.bind("<Enter>", self.show)
            widget.bind("<Leave>", self.hide)

        def show(self, _):
            self.top = tk.Toplevel(self.w)
            self.top.wm_overrideredirect(True)
            self.top.geometry(f"+{self.w.winfo_rootx() + 10}+{self.w.winfo_rooty() + self.w.winfo_height() + 2}")
            tk.Label(self.top, text=self.text, bg="#ffffe0", relief="solid", bd=1, padx=4).pack()

        def hide(self, _):
            if self.top:
                self.top.destroy()
                self.top = None

    # --- background work: worker threads post (callback, result, error) back to the UI thread
    def run_bg(work, done, label):
        state["busy"] = True
        set_buttons("disabled")
        status.set(label)

        def target():
            try:
                results.put((done, work(), None))
            except Exception as ex:
                results.put((done, None, ex))
        threading.Thread(target=target, daemon=True).start()

    def poll():
        try:
            while True:
                done, res, err = results.get_nowait()
                state["busy"] = False
                set_buttons("normal")
                done(res, err)
        except queue.Empty:
            pass
        root.after(100, poll)

    # --- top bar
    top = ttk.Frame(root, padding=(8, 8, 8, 4))
    top.pack(fill="x")
    b_check = ttk.Button(top, text="⟳", width=3, command=lambda: do_check())
    b_all = ttk.Button(top, text="⇪", width=3, command=lambda: do_update_all())
    b_open = ttk.Button(top, text="▤", width=3, command=lambda: os.startfile(reg["addons_dir"]))
    for b, tip in ((b_check, "Rescan folder and check for updates"),
                   (b_all, "Update all"), (b_open, "Open AddOns folder")):
        b.pack(side="left", padx=(0, 4))
        Tip(b, tip)
    status = tk.StringVar(value="")
    ttk.Label(top, textvariable=status, foreground="#555").pack(side="left", padx=8)

    # --- addon list (scrollable)
    mid = ttk.Frame(root, padding=(8, 0))
    mid.pack(fill="both", expand=True)
    canvas = tk.Canvas(mid, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
    rows = ttk.Frame(canvas)
    rows.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    win = canvas.create_window((0, 0), window=rows, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

    # --- install bar
    bot = ttk.Frame(root, padding=8)
    bot.pack(fill="x")
    ttk.Label(bot, text="CurseForge link").grid(row=0, column=0, sticky="w")
    ttk.Label(bot, text="Project ID").grid(row=0, column=1, sticky="w", padx=6)
    link_var, pid_var = tk.StringVar(), tk.StringVar()
    e_link = ttk.Entry(bot, textvariable=link_var)
    e_link.grid(row=1, column=0, sticky="ew")
    e_pid = ttk.Entry(bot, textvariable=pid_var, width=12)
    e_pid.grid(row=1, column=1, padx=6)
    b_install = ttk.Button(bot, text="⬇ Install", command=lambda: do_install())
    b_install.grid(row=1, column=2)
    bot.columnconfigure(0, weight=1)
    Tip(e_pid, "The 'Project ID' number in the About box on the addon's CurseForge page")

    def on_link_change(*_):  # known addon? fill in its Project ID
        try:
            slug, _f = parse_link(link_var.get())
        except ValueError:
            return
        for pid, e in reg["addons"].items():
            if e.get("slug") == slug and not pid_var.get().strip():
                pid_var.set(pid)
    link_var.trace_add("write", on_link_change)

    action_buttons = []

    def set_buttons(st):
        for b in [b_check, b_all, b_install] + action_buttons:
            try:
                b.configure(state=st)
            except tk.TclError:
                pass

    def refresh(untracked=()):
        action_buttons.clear()
        for w in rows.winfo_children():
            w.destroy()
        hdr = {"foreground": "#777"}
        for c, t in enumerate(("Addon", "Installed", "Latest (Forever)", "", "")):
            ttk.Label(rows, text=t, **hdr).grid(row=0, column=c, sticky="w", padx=4, pady=(0, 2))
        rows.columnconfigure(0, weight=1)
        items = sorted(reg["addons"].items(), key=lambda kv: kv[1]["name"].lower())
        for r, (pid, e) in enumerate(items, start=1):
            due = update_due(e)
            name = e["name"] + ("  (incomplete)" if e.get("missing") else "")
            ttk.Label(rows, text=name).grid(row=r, column=0, sticky="w", padx=4)
            ttk.Label(rows, text=e.get("version", "?")).grid(row=r, column=1, sticky="w", padx=4)
            latest = e.get("latest_error") or (e["latest"]["version"] if e.get("latest") else "…")
            ttk.Label(rows, text=latest, foreground="#c60" if due else ("#a00" if e.get("latest_error") else "#383"))\
                .grid(row=r, column=2, sticky="w", padx=4)
            if due:
                b = ttk.Button(rows, text="⬆", width=3, command=lambda p=pid: do_update(p))
                b.grid(row=r, column=3, padx=2)
                Tip(b, "Update to " + e["latest"]["version"])
                action_buttons.append(b)
            b = ttk.Button(rows, text="✕", width=3, command=lambda p=pid: do_uninstall(p))
            b.grid(row=r, column=4, padx=2)
            Tip(b, "Uninstall (delete folders)")
            action_buttons.append(b)
        if not items:
            ttk.Label(rows, text="No addons registered yet - paste a CurseForge link below.", foreground="#777")\
                .grid(row=1, column=0, columnspan=5, sticky="w", padx=4, pady=6)
        if untracked:
            ttk.Label(rows, text="Untracked (no CurseForge ID): " + ", ".join(untracked),
                      foreground="#999", wraplength=640, justify="left")\
                .grid(row=len(items) + 2, column=0, columnspan=5, sticky="w", padx=4, pady=(10, 0))
        state["untracked"] = list(untracked)

    def fail(err):
        status.set("Error")
        messagebox.showerror("Addon manager", str(err))

    def summary():
        n = sum(update_due(e) for e in reg["addons"].values())
        return f"{n} update(s) available" if n else "All up to date"

    def do_check(scan_msg=""):
        def work():
            rep = scan(reg)
            save_register(reg)
            errs = check_updates(reg, prefix)
            save_register(reg)
            return rep, errs

        def done(res, err):
            if err:
                refresh(state.get("untracked", ()))
                return fail(err)
            rep, errs = res
            refresh(rep["untracked"])
            notes = []
            if rep["removed"]:
                notes.append("removed: " + ", ".join(rep["removed"]))
            if rep["adopted"]:
                notes.append("found: " + ", ".join(rep["adopted"]))
            if errs:
                notes.append("check failed: " + ", ".join(errs))
            status.set(" · ".join([summary()] + notes))
        run_bg(work, done, "Scanning and checking for updates…")

    def install_done(res, err):
        if err:
            return fail(err)
        refresh(state.get("untracked", ()))
        status.set(f"Installed {res['name']} {res['version']} · " + summary())
        reveal(Path(reg["addons_dir"]) / res["folders"][0])

    def do_install():
        try:
            slug, file_id = parse_link(link_var.get())
        except ValueError as ex:
            return fail(ex)
        pid_text = pid_var.get().strip()
        if not pid_text.isdigit():
            return fail("Enter the addon's Project ID (number from the About box on its CurseForge page).")
        pid = int(pid_text)

        def resolved(meta, err):
            if err:
                return fail(err)
            if meta is None:
                return fail(f"No Forever-compatible ({prefix}.x) release found for this addon.")
            if not is_compatible(meta, prefix) and not messagebox.askokcancel(
                    "Not marked for Forever",
                    f"{meta['displayName']} is marked for {', '.join(meta.get('gameVersions', []))}, "
                    f"not {prefix}.x. Install anyway?"):
                return status.set("Install cancelled")

            def finished(res, err2):
                if not err2:
                    link_var.set("")
                    pid_var.set("")
                install_done(res, err2)
            run_bg(lambda: install_file(reg, pid, meta, slug), finished, f"Installing {meta['fileName']}…")

        run_bg(lambda: get_file(pid, file_id) if file_id else latest_file(pid, prefix),
               resolved, "Looking up file…")

    def update_meta(e):
        return get_file(e["project_id"], e["latest"]["file_id"])

    def do_update(pid):
        e = reg["addons"][pid]
        run_bg(lambda: install_file(reg, e["project_id"], update_meta(e), e.get("slug")),
               install_done, f"Updating {e['name']}…")

    def do_update_all():
        due = [e for e in reg["addons"].values() if update_due(e)]
        if not due:
            return status.set("Nothing to update")

        def work():
            done_names, failed = [], []
            for e in due:
                try:
                    install_file(reg, e["project_id"], update_meta(e), e.get("slug"))
                    done_names.append(e["name"])
                except Exception as ex:
                    failed.append(f"{e['name']}: {ex}")
            return done_names, failed

        def done(res, err):
            refresh(state.get("untracked", ()))
            if err:
                return fail(err)
            ok, failed = res
            status.set(f"Updated {len(ok)} · " + summary())
            if failed:
                messagebox.showwarning("Some updates failed", "\n".join(failed))
        run_bg(work, done, f"Updating {len(due)} addon(s)…")

    def do_uninstall(pid):
        e = reg["addons"][pid]
        if not messagebox.askyesno("Uninstall", f"Delete {e['name']}?\n\nFolders: {', '.join(e['folders'])}"):
            return

        def done(res, err):
            refresh(state.get("untracked", ()))
            if err:
                return fail(err)
            status.set(f"Uninstalled {res['name']}")
        run_bg(lambda: uninstall(reg, pid), done, f"Removing {e['name']}…")

    def on_close():
        if state["busy"] and not messagebox.askyesno("Busy", "An operation is still running. Quit anyway?"):
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    poll()
    do_check()
    root.mainloop()


if __name__ == "__main__":
    run_ui()
