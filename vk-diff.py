# -*- coding: utf-8 -*-
"""
vk-diff — сверка локальной папки с фотоальбомом ВКонтакте и поиск дубликатов.
Без API-токена: открытый запрос photos.get, затем HTML-парсинг альбома
с постраничным обходом через ?offset=.
Хэши кэшируются в JSON, недостающие фото и дубликаты — в CSV,
недостающие фото и группы дубликатов — миниатюрами в окне.
Горячие клавиши (Ctrl+V/C/X/A, Shift+Insert) работают в любой раскладке:
обработчик вешается на сам виджет и определяет действие по коду клавиши,
имени события и символу (латиница/кириллица/управляющий символ).
Все файлы (кэш, лог, CSV) создаются рядом с запущенным .py или .exe/.app.
Все ошибки, включая падения внутри Tkinter, пишутся в vk_diff.log.
"""
import csv
import datetime
import html
import io
import json
import os
import re
import subprocess
import sys
import threading
import traceback
import tkinter as tk
import webbrowser
from pathlib import Path

import imagehash
import requests
from PIL import Image, ImageTk
from tkinter import filedialog, messagebox, ttk

# ---------- базовая папка: рядом с запущенным файлом ----------
# При запуске .py это папка со скриптом; при запуске .exe/.app (PyInstaller
# выставляет sys.frozen) — папка, где лежит сам исполняемый файл.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CACHE_FILE = BASE_DIR / "vk_diff_cache.json"
MISSING_CSV = BASE_DIR / "missing_photos.csv"
DUP_CSV = BASE_DIR / "duplicates.csv"
LOG_FILE = BASE_DIR / "vk_diff.log"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
}
DEFAULT_THRESHOLD = 8
DEFAULT_DUP_THRESHOLD = 5
THUMB = 140

USERAPI_RE = re.compile(r'https://[A-Za-z0-9.-]*userapi\.com/[^"\'\\\s<>()]+')

log_lock = threading.Lock()


def write_log(message: str):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    with log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    return line


def write_exception(exc, context: str = ""):
    lines = [f"--- ОШИБКА [{context}] ---"]
    lines.append(f"Python: {sys.version.split()[0]}, платформа: {sys.platform}")
    mode = "exe/app" if getattr(sys, "frozen", False) else "скрипт"
    lines.append(f"Режим: {mode}, папка файлов: {BASE_DIR}")
    try:
        lines.extend(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        lines.append(repr(exc))
    text = "\n".join(lines)
    with log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
    return text


# ---------- получение ссылок на фото из альбома ----------

def parse_album(url: str):
    m = re.search(r"album(-?\d+)_(\d+)", url or "")
    if not m:
        raise ValueError("Не похоже на ссылку на альбом.\n"
                         "Пример: https://vk.com/album12345_67890")
    return m.group(1), m.group(2)


def pick_url(sizes):
    for t in ("y", "x"):
        for s in sizes:
            if s.get("type") == t:
                return s["url"]
    return sizes[-1]["url"] if sizes else None


def fetch_album_via_api(owner, album, log):
    urls, offset = [], 0
    while True:
        try:
            r = requests.get(
                "https://api.vk.com/method/photos.get",
                params={"owner_id": owner, "album_id": album, "rev": 1,
                        "photo_sizes": 1, "offset": offset, "count": 1000,
                        "v": "5.131"},
                timeout=25).json()
        except Exception as e:
            write_exception(e, f"photos.get offset={offset}")
            raise
        if "error" in r:
            err = r["error"]
            write_log(f"API photos.get: ошибка №{err.get('error_code')} "
                      f"'{err.get('error_msg')}' (offset={offset})")
            log(f"API ответил ошибкой №{err.get('error_code')} — пробую HTML-парсинг")
            return None
        resp = r.get("response") or {}
        items = resp.get("items") or []
        if not items:
            break
        for it in items:
            u = pick_url(it.get("sizes") or [])
            if u:
                urls.append(u)
        offset += len(items)
        if offset >= resp.get("count", 0):
            break
        log(f"API: получено ссылок {len(urls)}")
    return urls or None


def extract_urls(text: str):
    return USERAPI_RE.findall(text)


def dedupe_urls(urls):
    """Фото уникально по пути без параметров — разные размеры одной
    картинки дают разные ссылки с одним путём."""
    seen = {}
    for u in urls:
        base = u.split("?")[0]
        if base not in seen:
            seen[base] = u
    return [seen[k] for k in seen]


def fetch_album_via_html(owner, album, log):
    urls = set()
    offset = 0
    step = 50
    max_offset = 100000
    empty_strikes = 0

    while offset <= max_offset:
        page = f"https://vk.com/album{owner}_{album}?offset={offset}"
        try:
            r = requests.get(page, headers=HEADERS, timeout=25)
            write_log(f"HTML {page}: HTTP {r.status_code}, {len(r.text)} байт")
            if r.status_code != 200:
                log(f"{page}: HTTP {r.status_code} — пропускаю порцию")
                offset += step
                continue
            text = html.unescape(r.text.replace("\\/", "/"))
            found = extract_urls(text)
            before = len(urls)
            urls.update(found)
            log(f"Смещение {offset}: найдено {len(found)} ссылок, "
                f"уникальных всего {len(urls)}")
            if len(urls) == before:
                empty_strikes += 1
                if empty_strikes >= 2:
                    break
            else:
                empty_strikes = 0
        except Exception as e:
            write_exception(e, f"HTML-парсинг {page}")
            log(f"{page}: ошибка — {e}")
            break
        offset += step
    return dedupe_urls(sorted(urls)) or None


# ---------- кластеризация дубликатов ----------

def cluster_duplicates(pairs, threshold):
    """pairs: [(url, ImageHash)]. Склеивает фото с расстоянием <= threshold
    в группы (union-find). Возвращает только группы из 2+ фото."""
    n = len(pairs)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if pairs[i][1] - pairs[j][1] <= threshold:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(pairs[i][0])
    return [sorted(g) for g in groups.values() if len(g) > 1]


# ---------- кэш хэшей ----------

class HashCache:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as e:
            write_exception(e, "чтение кэша")
            self.data = {}
        self.thumbs = {}

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            write_exception(e, "сохранение кэша")

    def local(self, file: Path):
        st = file.stat()
        key = f"L|{file.resolve()}|{st.st_size}|{int(st.st_mtime)}"
        if key not in self.data:
            with Image.open(file) as im:
                self.data[key] = str(imagehash.phash(im))
        return self.data[key]

    def remote(self, url: str):
        if url not in self.data:
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                fallback = url.split("?")[0]
                code = e.response.status_code if e.response is not None else "?"
                write_log(f"HTTP {code} для {url}, пробую без параметров: {fallback}")
                r = requests.get(fallback, headers=HEADERS, timeout=30)
                r.raise_for_status()
            with Image.open(io.BytesIO(r.content)) as im:
                self.data[url] = str(imagehash.phash(im))
                thumb = im.copy()
                thumb.thumbnail((THUMB, THUMB))
                self.thumbs.setdefault(url, thumb)
        return self.data[url]

    def get_thumb(self, url: str) -> Image.Image:
        img = self.thumbs.get(url)
        if img is None:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content))
            img.thumbnail((THUMB, THUMB))
            self.thumbs[url] = img
        return img


# ---------- сравнение ----------

def find_missing(album_hexes, local_pairs, threshold):
    album = [imagehash.hex_to_hash(h) for h in album_hexes]
    missing = []
    for path, h in local_pairs:
        lh = imagehash.hex_to_hash(h)
        if all(lh - ah > threshold for ah in album):
            missing.append(path)
    return missing


def open_file(path):
    try:
        path = Path(path)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        write_exception(e, f"открытие файла {path}")


# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("vk-diff — недостающие фото и дубликаты альбома")
        self.geometry("960x680")
        self.cache = HashCache(CACHE_FILE)
        self.missing = []
        self.dup_groups = []
        self._thumb_refs = []
        self.report_callback_exception = self._tk_exception_hook
        sys.excepthook = self._sys_excepthook

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)
        top.columnconfigure(0, weight=1)

        ttk.Label(top, text="Ссылка на альбом ВК:").grid(row=0, column=0, sticky="w")
        url_row = ttk.Frame(top)
        url_row.grid(row=1, column=0, sticky="we", pady=2)
        self.url_entry = ttk.Entry(url_row)
        self.url_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(url_row, text="Вставить",
                   command=lambda: self.do_paste(self.url_entry)).pack(side="left", padx=4)
        self.bind_entry_shortcuts(self.url_entry)

        ttk.Label(top, text="Папка с фото:").grid(row=2, column=0, sticky="w")
        row = ttk.Frame(top)
        row.grid(row=3, column=0, sticky="we", pady=2)
        self.dir_entry = ttk.Entry(row)
        self.dir_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор…", command=self.choose_dir).pack(side="left", padx=4)
        self.bind_entry_shortcuts(self.dir_entry)

        controls = ttk.Frame(top)
        controls.grid(row=4, column=0, sticky="we", pady=6)
        ttk.Label(controls, text="Порог похожести:").pack(side="left")
        self.thresh = tk.IntVar(value=DEFAULT_THRESHOLD)
        ttk.Spinbox(controls, from_=2, to=24, width=4,
                    textvariable=self.thresh).pack(side="left", padx=(2, 10))
        ttk.Label(controls, text="Порог дубликатов:").pack(side="left")
        self.dup_thresh = tk.IntVar(value=DEFAULT_DUP_THRESHOLD)
        ttk.Spinbox(controls, from_=0, to=12, width=3,
                    textvariable=self.dup_thresh).pack(side="left", padx=(2, 10))

        self.run_btn = ttk.Button(controls, text="Сравнить", command=self.run_compare)
        self.run_btn.pack(side="left", padx=4)
        self.dup_btn = ttk.Button(controls, text="Найти дубликаты",
                                  command=self.run_duplicates)
        self.dup_btn.pack(side="left", padx=4)
        ttk.Button(controls, text="Недостающие в CSV",
                   command=self.save_csv).pack(side="left", padx=4)
        ttk.Button(controls, text="Дубликаты в CSV",
                   command=self.save_duplicates_csv).pack(side="left", padx=4)
        ttk.Button(controls, text="Открыть лог",
                   command=lambda: open_file(LOG_FILE)).pack(side="left", padx=4)

        self.status = ttk.Label(self, text="Готово к работе")
        self.status.pack(fill="x", padx=10)

        area = ttk.Frame(self)
        area.pack(fill="both", expand=True, padx=10, pady=8)
        self.canvas = tk.Canvas(area, highlightthickness=0)
        vsb = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(-e.delta // 120, "units"))

    # ----- страховочные перехватчики -----

    def _tk_exception_hook(self, etype, evalue, tb):
        write_exception(evalue, f"колбэк Tkinter ({etype.__name__})")
        try:
            self.status.config(text=f"Ошибка: {evalue} (подробности в {LOG_FILE.name})")
        except Exception:
            pass
        try:
            messagebox.askyesno(
                "vk-diff — ошибка",
                f"Что-то не получилось: {evalue}\n\nОткрыть файл лога с подробностями?")
        except Exception:
            pass

    def _sys_excepthook(self, etype, evalue, tb):
        write_exception(evalue, "необработанное исключение")

    # ----- буфер обмена -----

    def do_copy(self, entry):
        try:
            text = entry.selection_get()
        except tk.TclError:
            text = ""
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def do_paste(self, entry):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self.status.config(text="Буфер обмена пуст или недоступен")
            return
        try:
            start = entry.index("sel.first")
            end = entry.index("sel.last")
            entry.delete(start, end)
        except tk.TclError:
            pass
        entry.insert("insert", text)

    def bind_entry_shortcuts(self, entry):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Вставить", command=lambda: self.do_paste(entry))
        menu.add_command(label="Копировать", command=lambda: self.do_copy(entry))

        def popup(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        entry.bind("<Button-3>", popup)
        entry.bind("<Button-2>", popup)

        # Горячие клавиши определяются по нескольким сигналам сразу.
        # Коды клавиш на Windows (не зависят от раскладки):
        # V=86, C=67, X=88, A=65, Insert=45.
        CODE2ACTION = {86: "v", 67: "c", 88: "x", 65: "a"}
        # Кириллические символы на тех же клавишах (ЙЦУКЕН): V→М, C→С, X→Ч, A→Ф
        CYR2LAT = {"м": "v", "с": "c", "ч": "x", "ф": "a"}
        # Управляющие символы Ctrl+латиница: Ctrl+V=\x16, Ctrl+C=\x03,
        # Ctrl+X=\x18, Ctrl+A=\x01
        CTRL2CHAR = {"\x16": "v", "\x03": "c", "\x18": "x", "\x01": "a"}
        CTRL_FLAG = 0x0004
        SHIFT_FLAG = 0x0001

        def resolve(event):
            action = CODE2ACTION.get(event.keycode)
            if action:
                return action
            sym = (event.keysym or "").lower()
            if sym in ("v", "c", "x", "a"):
                return sym
            ch = (event.char or "").lower()
            if ch in CYR2LAT:
                return CYR2LAT[ch]
            if ch in ("v", "c", "x", "a"):
                return ch
            return CTRL2CHAR.get(event.char)

        def key_handler(event):
            ctrl = bool(event.state & CTRL_FLAG)
            if not ctrl:
                # Shift+Insert — запасная вставка, работает в любой раскладке
                if event.keycode == 45 and event.state & SHIFT_FLAG:
                    self.do_paste(entry)
                    return "break"
                return  # обычный ввод символов — не вмешиваемся
            action = resolve(event)
            if action == "v":
                self.do_paste(entry)
                return "break"
            if action == "c":
                self.do_copy(entry)
                return "break"
            if action == "x":
                self.do_copy(entry)
                try:
                    entry.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                return "break"
            if action == "a":
                entry.select_range(0, "end")
                entry.icursor("end")
                return "break"
            # Диагностика: незнакомое Ctrl-сочетание — в лог
            write_log(f"Ctrl-сочетание не распознано: keycode={event.keycode}, "
                      f"keysym={event.keysym!r}, char={event.char!r}, state={event.state}")

        # Бинд именно на сам виджет: "<KeyPress>" ловит события и с
        # кириллическими именами, а "break" отключает встроенную обработку
        # Entry — вставка не дублируется ни в одной раскладке.
        entry.bind("<KeyPress>", key_handler)

    # ----- служебное -----

    def log(self, msg):
        write_log(msg)
        self.after(0, lambda m=msg: self.status.config(text=m))

    def choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)

    def clear_grid(self):
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self._thumb_refs = []

    def report_error(self, exc: Exception, context: str):
        write_exception(exc, context)
        self.log(f"Ошибка: {exc} (подробности в {LOG_FILE.name})")
        self.after(0, lambda: messagebox.askyesno(
            "vk-diff — ошибка",
            f"Что-то не получилось: {exc}\n\nОткрыть файл лога с подробностями?"))

    def get_album_urls(self, log):
        """Общая подготовка для обеих кнопок: получить ссылки на фото альбома."""
        url = self.url_entry.get()
        owner, album = parse_album(url)
        log("Получаю список фото из альбома…")
        urls = fetch_album_via_api(owner, album, log)
        if urls is None:
            urls = fetch_album_via_html(owner, album, log)
        if not urls:
            log("Не удалось получить фото из альбома: возможно, он закрыт, "
                "или ВК изменил вёрстку")
        return urls

    # ----- основной сценарий: недостающие фото -----

    def run_compare(self):
        folder = Path(self.dir_entry.get())
        if not folder.is_dir():
            messagebox.showerror("Ошибка", "Папка не найдена")
            return
        try:
            owner, album = parse_album(self.url_entry.get())
        except ValueError as e:
            write_exception(e, f"разбор ссылки: {self.url_entry.get()!r}")
            messagebox.showerror("Ошибка", str(e))
            return
        self.run_btn.config(state="disabled")
        self.dup_btn.config(state="disabled")
        self.clear_grid()
        write_log(f"=== Сравнение: album{owner}_{album}, папка {folder}, "
                  f"порог {self.thresh.get()} ===")
        threading.Thread(target=self._compare_worker, args=(folder,),
                         daemon=True).start()

    def _compare_worker(self, folder):
        try:
            urls = self.get_album_urls(self.log)
            if not urls:
                return
            self.log(f"Фото в альбоме: {len(urls)}. Считаю хэши (первый раз медленно)…")

            album_hexes, failed = [], []
            for i, u in enumerate(urls, 1):
                try:
                    album_hexes.append(self.cache.remote(u))
                except Exception as e:
                    write_exception(e, f"скачивание фото альбома {u}")
                    failed.append(u)
                if i % 20 == 0:
                    self.log(f"Фото альбома: {i}/{len(urls)}")
            if failed:
                self.log(f"Не удалось обработать фото из альбома: {len(failed)} "
                         f"(ссылки в логе)")

            self.log("Считаю хэши локальных фото…")
            exts = {".jpg", ".jpeg", ".png", ".webp"}
            files = [p for p in folder.iterdir() if p.suffix.lower() in exts]
            local_pairs, bad_local = [], []
            for i, f in enumerate(files, 1):
                try:
                    local_pairs.append((f, self.cache.local(f)))
                except Exception as e:
                    write_exception(e, f"локальный файл {f}")
                    bad_local.append(f.name)
                if i % 20 == 0:
                    self.log(f"Локальные фото: {i}/{len(files)}")
            self.cache.save()
            if bad_local:
                self.log(f"Пропущено локальных файлов: {len(bad_local)} (список в логе)")
                write_log("Пропущенные файлы: " + ", ".join(bad_local))

            missing = find_missing(album_hexes, local_pairs, self.thresh.get())
            self.missing = missing
            self.dup_groups = []
            write_log(f"Итог сравнения: не загружено {len(missing)} из {len(local_pairs)}")
            self.after(0, self.show_missing)
        except Exception as e:
            self.report_error(e, "сравнение с папкой")
        finally:
            self.after(0, lambda: (self.run_btn.config(state="normal"),
                                   self.dup_btn.config(state="normal")))

    # ----- поиск дубликатов в альбоме -----

    def run_duplicates(self):
        try:
            parse_album(self.url_entry.get())
        except ValueError as e:
            write_exception(e, f"разбор ссылки: {self.url_entry.get()!r}")
            messagebox.showerror("Ошибка", str(e))
            return
        self.run_btn.config(state="disabled")
        self.dup_btn.config(state="disabled")
        self.clear_grid()
        write_log(f"=== Поиск дубликатов: {self.url_entry.get()}, "
                  f"порог {self.dup_thresh.get()} ===")
        threading.Thread(target=self._dup_worker, daemon=True).start()

    def _dup_worker(self):
        try:
            urls = self.get_album_urls(self.log)
            if not urls:
                return
            self.log(f"Фото в альбоме: {len(urls)}. Считаю хэши (кэш ускорит)…")

            pairs, failed = [], []
            for i, u in enumerate(urls, 1):
                try:
                    pairs.append((u, imagehash.hex_to_hash(self.cache.remote(u))))
                except Exception as e:
                    write_exception(e, f"скачивание фото альбома {u}")
                    failed.append(u)
                if i % 20 == 0:
                    self.log(f"Хэши: {i}/{len(urls)}")
            self.cache.save()

            self.log("Ищу похожие пары…")
            groups = cluster_duplicates(pairs, self.dup_thresh.get())
            total_dupes = sum(len(g) for g in groups)
            write_log(f"Итог дубликатов: {len(groups)} групп, {total_dupes} фото")

            if not groups:
                self.dup_groups = []
                self.log("Дубликатов не найдено ✔")
                return

            self.log(f"Групп дубликатов: {len(groups)}. Загружаю превью…")
            for gi, g in enumerate(groups, 1):
                for u in g:
                    try:
                        self.cache.get_thumb(u)
                    except Exception as e:
                        write_exception(e, f"превью дубликата {u}")
                if gi % 5 == 0:
                    self.log(f"Превью групп: {gi}/{len(groups)}")

            self.dup_groups = groups
            self.after(0, self.show_duplicates)
        except Exception as e:
            self.report_error(e, "поиск дубликатов")
        finally:
            self.after(0, lambda: (self.run_btn.config(state="normal"),
                                   self.dup_btn.config(state="normal")))

    def show_duplicates(self):
        self.clear_grid()
        if not self.dup_groups:
            self.status.config(text="Дубликатов не найдено ✔")
            return
        self.status.config(
            text=f"Найдено групп дубликатов: {len(self.dup_groups)}. "
                 f"Клик по миниатюре — открыть фото в браузере")
        row = 0
        for gi, group in enumerate(self.dup_groups, 1):
            ttk.Label(self.grid_frame,
                      text=f"Группа {gi} — {len(group)} похожих фото",
                      font=("", 10, "bold")).grid(
                row=row, column=0, columnspan=6, sticky="w", pady=(8, 2))
            row += 1
            for ci, u in enumerate(group, 1):
                cell = ttk.Frame(self.grid_frame, padding=4)
                cell.grid(row=row, column=ci - 1, sticky="w")
                try:
                    img = self.cache.get_thumb(u)
                    photo = ImageTk.PhotoImage(img)
                    self._thumb_refs.append(photo)
                    tk.Button(cell, image=photo, bd=0,
                              command=lambda link=u: webbrowser.open(link)).pack()
                except Exception as e:
                    write_exception(e, f"превью дубликата {u}")
                    tk.Label(cell, text="⚠").pack()
                ttk.Label(cell, text=Path(u).name[:30], width=22,
                          wraplength=THUMB).pack()
            row += 1

    def show_missing(self):
        self.clear_grid()
        if not self.missing:
            self.status.config(text="Все фото из папки есть в альбоме ✔")
            return
        self.status.config(
            text=f"Не загружено: {len(self.missing)}. Клик по миниатюре — открыть файл")
        for idx, path in enumerate(self.missing):
            r, c = divmod(idx, 5)
            cell = ttk.Frame(self.grid_frame, padding=4)
            cell.grid(row=r, column=c)
            try:
                with Image.open(path) as im:
                    im.thumbnail((THUMB, THUMB))
                    photo = ImageTk.PhotoImage(im)
                self._thumb_refs.append(photo)
                tk.Button(cell, image=photo, bd=0,
                          command=lambda p=path: open_file(p)).pack()
            except Exception as e:
                write_exception(e, f"превью {path}")
                tk.Label(cell, text="⚠").pack()
            ttk.Label(cell, text=path.name, width=22, wraplength=THUMB).pack()

    def save_csv(self):
        if not self.missing:
            messagebox.showinfo("vk-diff", "Список пуст — нечего сохранять")
            return
        try:
            with open(MISSING_CSV, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Имя файла", "Полный путь"])
                for p in self.missing:
                    w.writerow([p.name, str(p)])
        except Exception as e:
            self.report_error(e, "сохранение CSV")
            return
        self.status.config(text=f"Сохранено: {MISSING_CSV}")
        messagebox.showinfo("vk-diff", f"Файл сохранён:\n{MISSING_CSV}")

    def save_duplicates_csv(self):
        if not self.dup_groups:
            messagebox.showinfo("vk-diff",
                                "Дубликаты не искали или их нет — нечего сохранять")
            return
        try:
            with open(DUP_CSV, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Группа", "Ссылка"])
                for gi, group in enumerate(self.dup_groups, 1):
                    for u in group:
                        w.writerow([gi, u])
        except Exception as e:
            self.report_error(e, "сохранение дубликатов CSV")
            return
        self.status.config(text=f"Сохранено: {DUP_CSV}")
        messagebox.showinfo("vk-diff", f"Файл сохранён:\n{DUP_CSV}")


def main():
    write_log("=== Запуск приложения ===")
    app = App()
    try:
        app.mainloop()
    except Exception as e:
        write_exception(e, "mainloop")
        raise


if __name__ == "__main__":
    main()
