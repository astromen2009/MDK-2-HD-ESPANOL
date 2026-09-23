#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 MDK2 .STR Editor  -  Editor completo de archivos de texto/dialogo de MDK2
================================================================================

FORMATO DEL ARCHIVO (deducido de mdk2.str / mdk2_es.str, round-trip byte-exacto)

  CABECERA (24 bytes, todo uint32 little-endian)
    0x00  magic          = 2003   (0x07D3)
    0x04  version/flags  = 2
    0x08  num_records    = 697    (cantidad de entradas de texto)
    0x0C  table_offset   = 24     (donde empieza la tabla de registros)
    0x10  text_offset    = 8388   (donde empieza el blob de texto UTF-16LE)
    0x14  audio_offset   = 74924  (donde empieza la tabla de nombres de audio)

  TABLA DE REGISTROS (12 bytes por registro, int32 con signo)
    +0x00  string_id      -> ID que usa el motor del juego para pedir el texto
    +0x04  text_offset    -> desplazamiento relativo al blob de texto, o -1 = sin texto
    +0x08  audio_ref      -> desplazamiento en bytes dentro de la tabla de audio
                            (= indice * 16), o -1 = sin audio asociado

  BLOB DE TEXTO
    Cadenas UTF-16 little-endian, terminadas en 0x0000, una detras de otra.
    Pueden contener saltos de linea (0x000A).

  TABLA DE AUDIO
    Registros de 16 bytes: nombre ASCII rellenado con ceros (max 15 caracteres).
    Ej: "ml1z_kurt1", "shwang_taunt1", "dr_booze".

--------------------------------------------------------------------------------
QUE SE PUEDE EDITAR CON ESTE PROGRAMA
  - El texto de cada entrada (lo principal)
  - Si una entrada tiene texto o no (estado "sin texto" = -1)
  - El ID numerico de cada entrada
  - Que audio tiene asignado cada entrada (o ninguno)
  - Los nombres de la tabla de audio (max 15 caracteres)
  - Anadir y borrar entradas
  - Los campos magic / version de la cabecera (pestana Cabecera)
  Todo lo demas (offsets, tamanos, punteros) se recalcula solo al guardar.

USO
  Interfaz grafica:   python mdk2_str_editor.py  [archivo.str]  [referencia.str]
  Linea de comandos:
      python mdk2_str_editor.py info    archivo.str
      python mdk2_str_editor.py export  archivo.str salida.csv  [referencia.str]
      python mdk2_str_editor.py import  archivo.str entrada.csv nuevo.str
      python mdk2_str_editor.py verify  archivo.str      (prueba de round-trip)

Requisitos: solo Python 3.8+ (tkinter viene incluido en Windows y macOS;
en Linux: sudo apt install python3-tk).
================================================================================
"""

import csv
import os
import struct
import sys

APP_NAME = "MDK2 .STR Editor"
APP_VER = "1.0"

HEADER_SIZE = 24
RECORD_SIZE = 12
AUDIO_ENTRY_SIZE = 16
AUDIO_NAME_MAX = AUDIO_ENTRY_SIZE - 1  # 15 caracteres + terminador
NO_VALUE = -1


# =============================================================================
#  NUCLEO: lectura / escritura del formato
# =============================================================================

class StrError(Exception):
    pass


class Record:
    """Una entrada de la tabla: ID + texto (opcional) + audio (opcional)."""

    __slots__ = ("sid", "text", "audio")

    def __init__(self, sid, text=None, audio=NO_VALUE):
        self.sid = int(sid)
        self.text = text          # str, o None si la entrada no tiene texto
        self.audio = int(audio)   # indice en la tabla de audio, o -1

    def __repr__(self):
        return "Record(id=%d, audio=%d, text=%r)" % (self.sid, self.audio, self.text)


class StrFile:
    """Representa un archivo .str completo, ya descomprimido a objetos Python."""

    def __init__(self):
        self.magic = 2003
        self.version = 2
        self.records = []       # lista de Record
        self.audio_names = []   # lista de str
        self.path = None

    # ---------------------------------------------------------------- lectura
    @classmethod
    def load(cls, path):
        with open(path, "rb") as fh:
            data = fh.read()
        obj = cls.from_bytes(data)
        obj.path = path
        return obj

    @classmethod
    def from_bytes(cls, data):
        if len(data) < HEADER_SIZE:
            raise StrError("El archivo es demasiado pequeno para ser un .str valido.")

        magic, version, count, table_off, text_off, audio_off = struct.unpack_from("<6I", data, 0)

        if magic != 2003:
            raise StrError(
                "Magic inesperado: %d (se esperaba 2003). "
                "Puede que no sea un .str de MDK2." % magic
            )
        if table_off + count * RECORD_SIZE != text_off:
            raise StrError("La tabla de registros no encaja con el inicio del texto (archivo corrupto?).")
        if not (text_off <= audio_off <= len(data)):
            raise StrError("Los offsets de la cabecera estan fuera de rango.")
        if (len(data) - audio_off) % AUDIO_ENTRY_SIZE != 0:
            raise StrError("La tabla de audio no es multiplo de 16 bytes.")

        obj = cls()
        obj.magic = magic
        obj.version = version

        # --- nombres de audio
        n_audio = (len(data) - audio_off) // AUDIO_ENTRY_SIZE
        for i in range(n_audio):
            raw = data[audio_off + i * AUDIO_ENTRY_SIZE: audio_off + (i + 1) * AUDIO_ENTRY_SIZE]
            obj.audio_names.append(raw.split(b"\0")[0].decode("latin-1"))

        # --- blob de texto
        blob = data[text_off:audio_off]

        def read_string(rel):
            if rel < 0 or rel >= len(blob):
                raise StrError("Offset de texto invalido: %d" % rel)
            end = rel
            while end + 1 < len(blob) and blob[end:end + 2] != b"\0\0":
                end += 2
            return blob[rel:end].decode("utf-16-le")

        # --- registros
        for i in range(count):
            sid, toff, aref = struct.unpack_from("<iii", data, table_off + i * RECORD_SIZE)
            text = None if toff < 0 else read_string(toff)
            if aref < 0:
                aidx = NO_VALUE
            else:
                if aref % AUDIO_ENTRY_SIZE != 0:
                    raise StrError("Referencia de audio no alineada en el registro %d." % i)
                aidx = aref // AUDIO_ENTRY_SIZE
                if aidx >= n_audio:
                    raise StrError("Referencia de audio fuera de rango en el registro %d." % i)
            obj.records.append(Record(sid, text, aidx))

        return obj

    # --------------------------------------------------------------- escritura
    def to_bytes(self, sort_by_id=True):
        """Reconstruye el archivo binario. Recalcula todos los offsets."""
        self.validate()

        records = sorted(self.records, key=lambda r: r.sid) if sort_by_id else list(self.records)

        # blob de texto, en el mismo orden que los registros (igual que el original)
        blob = bytearray()
        offsets = []
        for rec in records:
            if rec.text is None:
                offsets.append(NO_VALUE)
            else:
                offsets.append(len(blob))
                blob += rec.text.encode("utf-16-le") + b"\0\0"

        # tabla de audio
        audio_blob = bytearray()
        for name in self.audio_names:
            raw = name.encode("latin-1", errors="replace")[:AUDIO_NAME_MAX]
            audio_blob += raw + b"\0" * (AUDIO_ENTRY_SIZE - len(raw))

        table_off = HEADER_SIZE
        text_off = table_off + len(records) * RECORD_SIZE
        audio_off = text_off + len(blob)

        out = bytearray()
        out += struct.pack("<6I", self.magic, self.version, len(records),
                           table_off, text_off, audio_off)
        for rec, toff in zip(records, offsets):
            aref = NO_VALUE if rec.audio < 0 else rec.audio * AUDIO_ENTRY_SIZE
            out += struct.pack("<iii", rec.sid, toff, aref)
        out += blob
        out += audio_blob
        return bytes(out)

    def save(self, path):
        data = self.to_bytes()
        with open(path, "wb") as fh:
            fh.write(data)
        self.path = path
        return len(data)

    # -------------------------------------------------------------- utilidades
    def validate(self):
        if not self.records:
            raise StrError("No hay ninguna entrada que guardar.")
        seen = set()
        for rec in self.records:
            if rec.sid < 0:
                raise StrError("Hay un ID negativo (%d). Los IDs deben ser >= 0." % rec.sid)
            if rec.sid in seen:
                raise StrError("El ID %d esta repetido. Los IDs deben ser unicos." % rec.sid)
            seen.add(rec.sid)
            if rec.audio >= len(self.audio_names):
                raise StrError("El ID %d apunta a un audio inexistente (%d)." % (rec.sid, rec.audio))
        for name in self.audio_names:
            if len(name) > AUDIO_NAME_MAX:
                raise StrError("El nombre de audio '%s' supera los %d caracteres." % (name, AUDIO_NAME_MAX))

    def audio_name(self, idx):
        if idx is None or idx < 0 or idx >= len(self.audio_names):
            return ""
        return self.audio_names[idx]

    def by_id(self):
        return {r.sid: r for r in self.records}

    def audio_usage(self):
        """Cuantas entradas usan cada audio."""
        usage = [0] * len(self.audio_names)
        for rec in self.records:
            if 0 <= rec.audio < len(usage):
                usage[rec.audio] += 1
        return usage

    def stats(self):
        with_text = sum(1 for r in self.records if r.text is not None)
        with_audio = sum(1 for r in self.records if r.audio >= 0)
        chars = sum(len(r.text) for r in self.records if r.text)
        return {
            "entradas": len(self.records),
            "con_texto": with_text,
            "sin_texto": len(self.records) - with_text,
            "con_audio": with_audio,
            "audios": len(self.audio_names),
            "caracteres": chars,
        }


# =============================================================================
#  CSV: exportar / importar (para traducir en Excel, LibreOffice, etc.)
# =============================================================================

CSV_COLUMNS = ["id", "audio", "audio_idx", "original", "texto", "sin_texto"]


def export_csv(strf, path, reference=None, delimiter=";"):
    """Vuelca las entradas a un CSV editable. 'reference' es otro StrFile
    (p.ej. el ingles) del que se saca la columna 'original' por ID."""
    ref_map = reference.by_id() if reference else {}
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=delimiter, quoting=csv.QUOTE_ALL)
        w.writerow(CSV_COLUMNS)
        for rec in sorted(strf.records, key=lambda r: r.sid):
            ref = ref_map.get(rec.sid)
            w.writerow([
                rec.sid,
                strf.audio_name(rec.audio),
                rec.audio if rec.audio >= 0 else "",
                (ref.text if ref and ref.text is not None else ""),
                (rec.text if rec.text is not None else ""),
                "1" if rec.text is None else "0",
            ])


def import_csv(strf, path, delimiter=";"):
    """Aplica un CSV sobre un StrFile ya cargado. Solo toca las columnas
    'texto', 'sin_texto' y 'audio_idx'. Devuelve (aplicados, ignorados)."""
    index = strf.by_id()
    applied, skipped = 0, []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        r = csv.DictReader(fh, delimiter=delimiter)
        if not r.fieldnames or "id" not in r.fieldnames:
            raise StrError("El CSV no tiene una columna 'id'.")
        for row in r:
            try:
                sid = int(str(row["id"]).strip())
            except (TypeError, ValueError):
                continue
            rec = index.get(sid)
            if rec is None:
                skipped.append(sid)
                continue
            if str(row.get("sin_texto", "0")).strip() in ("1", "true", "True", "si", "sí"):
                rec.text = None
            else:
                rec.text = row.get("texto", "") or ""
            aidx = str(row.get("audio_idx", "")).strip()
            if aidx == "":
                rec.audio = NO_VALUE
            else:
                try:
                    rec.audio = int(aidx)
                except ValueError:
                    pass
            applied += 1
    return applied, skipped


# =============================================================================
#  INTERFAZ GRAFICA (tkinter)
# =============================================================================

def run_gui(initial=None, initial_ref=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    PREVIEW_LEN = 90

    def preview(text):
        if text is None:
            return "‹sin texto›"
        s = text.replace("\r", "").replace("\n", " ⏎ ")
        return s if len(s) <= PREVIEW_LEN else s[:PREVIEW_LEN] + "…"

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP_NAME)
            self.geometry("1280x780")
            self.minsize(900, 560)

            self.strf = None          # StrFile en edicion
            self.reference = None     # StrFile de referencia (ingles)
            self.original_texts = {}  # id -> texto original, para marcar modificados
            self.dirty = False
            self.current_id = None
            self.row_ids = []         # ids en el orden mostrado en la tabla

            self._build_menu()
            self._build_widgets()
            self._refresh_title()
            self.protocol("WM_DELETE_WINDOW", self.on_close)

        # ------------------------------------------------------------ montaje
        def _build_menu(self):
            m = tk.Menu(self)
            f = tk.Menu(m, tearoff=0)
            f.add_command(label="Abrir .str…", accelerator="Ctrl+O", command=self.open_file)
            f.add_command(label="Abrir .str de referencia (inglés)…", command=self.open_reference)
            f.add_separator()
            f.add_command(label="Guardar", accelerator="Ctrl+S", command=self.save_file)
            f.add_command(label="Guardar como…", command=self.save_file_as)
            f.add_separator()
            f.add_command(label="Exportar a CSV…", command=self.do_export_csv)
            f.add_command(label="Importar desde CSV…", command=self.do_import_csv)
            f.add_separator()
            f.add_command(label="Salir", command=self.on_close)
            m.add_cascade(label="Archivo", menu=f)

            e = tk.Menu(m, tearoff=0)
            e.add_command(label="Añadir entrada…", command=self.add_record)
            e.add_command(label="Borrar entrada seleccionada", command=self.delete_record)
            e.add_separator()
            e.add_command(label="Copiar texto de referencia en la traducción",
                          accelerator="Ctrl+R", command=self.copy_reference)
            e.add_command(label="Ir a ID…", accelerator="Ctrl+G", command=self.goto_id)
            m.add_cascade(label="Editar", menu=e)

            h = tk.Menu(m, tearoff=0)
            h.add_command(label="Acerca de", command=self.about)
            m.add_cascade(label="Ayuda", menu=h)
            self.config(menu=m)

            self.bind_all("<Control-o>", lambda ev: self.open_file())
            self.bind_all("<Control-s>", lambda ev: self.save_file())
            self.bind_all("<Control-g>", lambda ev: self.goto_id())
            self.bind_all("<Control-r>", lambda ev: self.copy_reference())

        def _build_widgets(self):
            nb = ttk.Notebook(self)
            nb.pack(fill="both", expand=True)
            self.tab_text = ttk.Frame(nb)
            self.tab_audio = ttk.Frame(nb)
            self.tab_head = ttk.Frame(nb)
            nb.add(self.tab_text, text="  Textos  ")
            nb.add(self.tab_audio, text="  Audios  ")
            nb.add(self.tab_head, text="  Cabecera / Info  ")

            self._build_text_tab()
            self._build_audio_tab()
            self._build_header_tab()

            self.status = tk.StringVar(value="Abre un archivo .str para empezar.")
            ttk.Label(self, textvariable=self.status, relief="sunken",
                      anchor="w", padding=(6, 3)).pack(fill="x", side="bottom")

        # --------------------------------------------------------- pestana 1
        def _build_text_tab(self):
            top = ttk.Frame(self.tab_text, padding=(6, 6, 6, 2))
            top.pack(fill="x")

            ttk.Label(top, text="Buscar:").pack(side="left")
            self.q = tk.StringVar()
            ent = ttk.Entry(top, textvariable=self.q, width=34)
            ent.pack(side="left", padx=(4, 10))
            self.q.trace_add("write", lambda *a: self.populate())

            self.f_mod = tk.BooleanVar(value=False)
            self.f_aud = tk.BooleanVar(value=False)
            self.f_emp = tk.BooleanVar(value=False)
            self.f_same = tk.BooleanVar(value=False)
            for txt, var in (("Solo modificados", self.f_mod),
                             ("Solo con audio", self.f_aud),
                             ("Solo sin texto", self.f_emp),
                             ("Igual a la referencia", self.f_same)):
                ttk.Checkbutton(top, text=txt, variable=var,
                                command=self.populate).pack(side="left", padx=4)

            paned = ttk.PanedWindow(self.tab_text, orient="vertical")
            paned.pack(fill="both", expand=True, padx=6, pady=(2, 6))

            # ---- tabla
            frame = ttk.Frame(paned)
            cols = ("id", "audio", "ref", "txt")
            self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
            for key, label, width, anchor in (
                ("id", "ID", 70, "e"),
                ("audio", "Audio (nombre)", 170, "w"),
                ("ref", "Texto original (referencia)", 440, "w"),
                ("txt", "Texto del .str (editable)", 440, "w"),
            ):
                self.tree.heading(key, text=label)
                self.tree.column(key, width=width, anchor=anchor,
                                 stretch=(key in ("ref", "txt")))
            vs = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vs.set)
            self.tree.pack(side="left", fill="both", expand=True)
            vs.pack(side="right", fill="y")
            self.tree.tag_configure("mod", background="#fff4d6")
            self.tree.tag_configure("none", foreground="#888888")
            self.tree.bind("<<TreeviewSelect>>", self.on_select)
            paned.add(frame, weight=3)

            # ---- editor
            ed = ttk.Frame(paned)
            bar = ttk.Frame(ed)
            bar.pack(fill="x", pady=(6, 4))
            ttk.Label(bar, text="ID:").pack(side="left")
            self.v_id = tk.StringVar()
            ttk.Entry(bar, textvariable=self.v_id, width=8).pack(side="left", padx=(4, 12))
            ttk.Label(bar, text="Audio:").pack(side="left")
            self.v_audio = tk.StringVar()
            self.cb_audio = ttk.Combobox(bar, textvariable=self.v_audio, width=34, state="readonly")
            self.cb_audio.pack(side="left", padx=(4, 12))
            self.v_none = tk.BooleanVar(value=False)
            ttk.Checkbutton(bar, text="Entrada sin texto (-1)", variable=self.v_none,
                            command=self._toggle_none).pack(side="left")
            ttk.Button(bar, text="Aplicar cambios (Ctrl+Enter)",
                       command=self.apply_changes).pack(side="right")
            ttk.Button(bar, text="Revertir", command=self.on_select).pack(side="right", padx=6)

            body = ttk.Frame(ed)
            body.pack(fill="both", expand=True)
            left = ttk.Frame(body)
            left.pack(side="left", fill="both", expand=True, padx=(0, 4))
            ttk.Label(left, text="Original / referencia (solo lectura)").pack(anchor="w")
            self.t_ref = tk.Text(left, height=7, wrap="word", background="#f2f2f2",
                                 state="disabled", font=("Segoe UI", 10))
            self.t_ref.pack(fill="both", expand=True)

            right = ttk.Frame(body)
            right.pack(side="left", fill="both", expand=True, padx=(4, 0))
            hdr = ttk.Frame(right)
            hdr.pack(fill="x")
            ttk.Label(hdr, text="Texto editable").pack(side="left")
            self.v_len = tk.StringVar(value="")
            ttk.Label(hdr, textvariable=self.v_len, foreground="#666").pack(side="right")
            self.t_txt = tk.Text(right, height=7, wrap="word", undo=True, font=("Segoe UI", 10))
            self.t_txt.pack(fill="both", expand=True)
            self.t_txt.bind("<KeyRelease>", self._update_len)
            self.t_txt.bind("<Control-Return>", lambda ev: (self.apply_changes(), "break")[1])
            paned.add(ed, weight=2)

        # --------------------------------------------------------- pestana 2
        def _build_audio_tab(self):
            top = ttk.Frame(self.tab_audio, padding=6)
            top.pack(fill="x")
            ttk.Label(top, text="Nombres de los clips de voz (máx. %d caracteres, sin extensión). "
                               "Cambiarlos hace que el juego busque otro archivo de sonido."
                               % AUDIO_NAME_MAX).pack(side="left")

            frame = ttk.Frame(self.tab_audio, padding=(6, 0, 6, 6))
            frame.pack(fill="both", expand=True)
            self.atree = ttk.Treeview(frame, columns=("idx", "name", "use"),
                                      show="headings", selectmode="browse")
            for key, label, width, anchor in (("idx", "Índice", 80, "e"),
                                              ("name", "Nombre", 320, "w"),
                                              ("use", "Usado por", 110, "e")):
                self.atree.heading(key, text=label)
                self.atree.column(key, width=width, anchor=anchor)
            avs = ttk.Scrollbar(frame, orient="vertical", command=self.atree.yview)
            self.atree.configure(yscrollcommand=avs.set)
            self.atree.pack(side="left", fill="both", expand=True)
            avs.pack(side="right", fill="y")
            self.atree.bind("<Double-1>", lambda ev: self.rename_audio())

            bar = ttk.Frame(self.tab_audio, padding=(6, 0, 6, 8))
            bar.pack(fill="x")
            ttk.Button(bar, text="Renombrar (doble clic)", command=self.rename_audio).pack(side="left")
            ttk.Button(bar, text="Añadir nombre…", command=self.add_audio).pack(side="left", padx=6)

        # --------------------------------------------------------- pestana 3
        def _build_header_tab(self):
            f = ttk.Frame(self.tab_head, padding=12)
            f.pack(fill="both", expand=True)
            ttk.Label(f, text="Campos de la cabecera (no los toques salvo que sepas lo que haces)",
                      font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            self.v_magic = tk.StringVar()
            self.v_version = tk.StringVar()
            ttk.Label(f, text="magic:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
            ttk.Entry(f, textvariable=self.v_magic, width=14).grid(row=1, column=1, sticky="w")
            ttk.Label(f, text="(original: 2003)").grid(row=1, column=2, sticky="w", padx=6)
            ttk.Label(f, text="version:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
            ttk.Entry(f, textvariable=self.v_version, width=14).grid(row=2, column=1, sticky="w")
            ttk.Label(f, text="(original: 2)").grid(row=2, column=2, sticky="w", padx=6)
            ttk.Button(f, text="Aplicar", command=self.apply_header).grid(row=3, column=1, sticky="w", pady=8)

            ttk.Separator(f, orient="horizontal").grid(row=4, column=0, columnspan=3,
                                                       sticky="ew", pady=10)
            self.info = tk.Text(f, height=18, width=90, state="disabled",
                                background="#fafafa", font=("Consolas", 9))
            self.info.grid(row=5, column=0, columnspan=3, sticky="nsew")
            f.rowconfigure(5, weight=1)
            f.columnconfigure(2, weight=1)

        # ------------------------------------------------------------ acciones
        def _refresh_title(self):
            name = os.path.basename(self.strf.path) if (self.strf and self.strf.path) else "(sin archivo)"
            self.title("%s  —  %s%s" % (APP_NAME, name, " *" if self.dirty else ""))

        def mark_dirty(self, flag=True):
            self.dirty = flag
            self._refresh_title()

        def open_file(self):
            p = filedialog.askopenfilename(title="Abrir archivo .str",
                                           filetypes=[("Archivos STR", "*.str"), ("Todos", "*.*")])
            if p:
                self.load_path(p)

        def load_path(self, p):
            try:
                self.strf = StrFile.load(p)
            except Exception as ex:
                messagebox.showerror("Error al abrir", str(ex))
                return
            self.original_texts = {r.sid: r.text for r in self.strf.records}
            self.mark_dirty(False)
            self.current_id = None
            self.v_magic.set(str(self.strf.magic))
            self.v_version.set(str(self.strf.version))
            self._reload_audio_combo()
            self.populate()
            self.populate_audio()
            self.populate_info()
            s = self.strf.stats()
            self.status.set("Cargado %s — %d entradas, %d con texto, %d con audio, %d nombres de audio."
                            % (os.path.basename(p), s["entradas"], s["con_texto"],
                               s["con_audio"], s["audios"]))

        def open_reference(self):
            p = filedialog.askopenfilename(title="Abrir .str de referencia (p. ej. el inglés)",
                                           filetypes=[("Archivos STR", "*.str"), ("Todos", "*.*")])
            if not p:
                return
            try:
                self.reference = StrFile.load(p)
            except Exception as ex:
                messagebox.showerror("Error al abrir la referencia", str(ex))
                return
            self.populate()
            self.status.set("Referencia cargada: %s (%d entradas)."
                            % (os.path.basename(p), len(self.reference.records)))

        def save_file(self):
            if not self.strf:
                return
            if not self.strf.path:
                return self.save_file_as()
            self._write(self.strf.path)

        def save_file_as(self):
            if not self.strf:
                return
            p = filedialog.asksaveasfilename(title="Guardar como", defaultextension=".str",
                                             filetypes=[("Archivos STR", "*.str")])
            if p:
                self._write(p)

        def _write(self, path):
            try:
                if os.path.exists(path):  # copia de seguridad la primera vez
                    bak = path + ".bak"
                    if not os.path.exists(bak):
                        with open(path, "rb") as a, open(bak, "wb") as b:
                            b.write(a.read())
                n = self.strf.save(path)
            except Exception as ex:
                messagebox.showerror("Error al guardar", str(ex))
                return
            self.original_texts = {r.sid: r.text for r in self.strf.records}
            self.mark_dirty(False)
            self.populate()
            self.populate_info()
            self.status.set("Guardado: %s (%d bytes)." % (path, n))

        def do_export_csv(self):
            if not self.strf:
                return
            p = filedialog.asksaveasfilename(title="Exportar a CSV", defaultextension=".csv",
                                             filetypes=[("CSV", "*.csv")])
            if not p:
                return
            try:
                export_csv(self.strf, p, self.reference)
            except Exception as ex:
                messagebox.showerror("Error al exportar", str(ex))
                return
            self.status.set("CSV exportado: %s" % p)

        def do_import_csv(self):
            if not self.strf:
                return
            p = filedialog.askopenfilename(title="Importar CSV", filetypes=[("CSV", "*.csv"), ("Todos", "*.*")])
            if not p:
                return
            try:
                applied, skipped = import_csv(self.strf, p)
            except Exception as ex:
                messagebox.showerror("Error al importar", str(ex))
                return
            self.mark_dirty(True)
            self.populate()
            msg = "Aplicadas %d filas." % applied
            if skipped:
                msg += " %d IDs no existían y se ignoraron." % len(skipped)
            self.status.set(msg)

        # ---------------------------------------------------------- la tabla
        def populate(self):
            if not self.strf:
                return
            sel = self.current_id
            self.tree.delete(*self.tree.get_children())
            self.row_ids = []
            q = self.q.get().strip().lower()
            ref_map = self.reference.by_id() if self.reference else {}

            for rec in sorted(self.strf.records, key=lambda r: r.sid):
                ref = ref_map.get(rec.sid)
                ref_text = ref.text if (ref and ref.text is not None) else None
                changed = self.original_texts.get(rec.sid, "\0__new__") != rec.text

                if self.f_mod.get() and not changed:
                    continue
                if self.f_aud.get() and rec.audio < 0:
                    continue
                if self.f_emp.get() and rec.text is not None:
                    continue
                if self.f_same.get() and not (ref_text is not None and rec.text == ref_text):
                    continue
                if q:
                    hay = " ".join(filter(None, [
                        str(rec.sid), self.strf.audio_name(rec.audio),
                        rec.text or "", ref_text or ""])).lower()
                    if q not in hay:
                        continue

                tags = []
                if changed:
                    tags.append("mod")
                if rec.text is None:
                    tags.append("none")
                self.tree.insert("", "end", iid=str(rec.sid),
                                 values=(rec.sid, self.strf.audio_name(rec.audio),
                                         preview(ref_text), preview(rec.text)),
                                 tags=tuple(tags))
                self.row_ids.append(rec.sid)

            if sel is not None and str(sel) in self.tree.get_children():
                self.tree.selection_set(str(sel))
            self.status.set("Mostrando %d de %d entradas." % (len(self.row_ids), len(self.strf.records)))

        def on_select(self, event=None):
            sel = self.tree.selection()
            if not sel:
                return
            sid = int(sel[0])
            rec = self.strf.by_id().get(sid)
            if rec is None:
                return
            self.current_id = sid
            self.v_id.set(str(rec.sid))
            self.v_audio.set(self._audio_label(rec.audio))
            self.v_none.set(rec.text is None)

            ref = self.reference.by_id().get(sid) if self.reference else None
            self.t_ref.configure(state="normal")
            self.t_ref.delete("1.0", "end")
            if ref is not None and ref.text is not None:
                self.t_ref.insert("1.0", ref.text)
            elif self.reference is None:
                self.t_ref.insert("1.0", "(carga un .str de referencia en Archivo → Abrir .str de referencia)")
            self.t_ref.configure(state="disabled")

            self.t_txt.delete("1.0", "end")
            if rec.text is not None:
                self.t_txt.insert("1.0", rec.text)
            self.t_txt.configure(state="disabled" if rec.text is None else "normal")
            self._update_len()

        def _toggle_none(self):
            self.t_txt.configure(state="disabled" if self.v_none.get() else "normal")

        def _update_len(self, event=None):
            n = len(self.t_txt.get("1.0", "end-1c"))
            ref = ""
            if self.reference and self.current_id is not None:
                r = self.reference.by_id().get(self.current_id)
                if r and r.text is not None:
                    ref = "  (original: %d)" % len(r.text)
            self.v_len.set("%d caracteres%s" % (n, ref))

        def apply_changes(self):
            if not self.strf or self.current_id is None:
                return
            rec = self.strf.by_id().get(self.current_id)
            if rec is None:
                return
            try:
                new_id = int(self.v_id.get().strip())
            except ValueError:
                messagebox.showwarning("ID inválido", "El ID debe ser un número entero.")
                return
            if new_id != rec.sid and new_id in self.strf.by_id():
                messagebox.showwarning("ID repetido", "Ya existe una entrada con el ID %d." % new_id)
                return

            rec.sid = new_id
            rec.audio = self._audio_index(self.v_audio.get())
            rec.text = None if self.v_none.get() else self.t_txt.get("1.0", "end-1c")
            self.current_id = new_id
            self.mark_dirty(True)
            self.populate()
            self.status.set("Entrada %d actualizada (aún sin guardar)." % new_id)

        def copy_reference(self):
            if not (self.reference and self.current_id is not None):
                return
            r = self.reference.by_id().get(self.current_id)
            if r is None or r.text is None:
                return
            self.v_none.set(False)
            self.t_txt.configure(state="normal")
            self.t_txt.delete("1.0", "end")
            self.t_txt.insert("1.0", r.text)
            self._update_len()

        def goto_id(self):
            if not self.strf:
                return
            val = simpledialog.askinteger("Ir a ID", "Número de ID:", parent=self)
            if val is None:
                return
            if str(val) in self.tree.get_children():
                self.tree.selection_set(str(val))
                self.tree.see(str(val))
            else:
                messagebox.showinfo("No encontrado", "El ID %d no está visible con los filtros actuales." % val)

        def add_record(self):
            if not self.strf:
                return
            new_id = max((r.sid for r in self.strf.records), default=0) + 1
            val = simpledialog.askinteger("Añadir entrada", "ID de la nueva entrada:",
                                          initialvalue=new_id, parent=self)
            if val is None:
                return
            if val in self.strf.by_id():
                messagebox.showwarning("ID repetido", "Ese ID ya existe.")
                return
            self.strf.records.append(Record(val, "", NO_VALUE))
            self.mark_dirty(True)
            self.populate()
            if str(val) in self.tree.get_children():
                self.tree.selection_set(str(val))
                self.tree.see(str(val))

        def delete_record(self):
            if not (self.strf and self.current_id is not None):
                return
            if not messagebox.askyesno("Borrar", "¿Borrar la entrada %d?\n\nSi el juego pide ese ID "
                                                 "puede fallar o mostrar texto vacío." % self.current_id):
                return
            self.strf.records = [r for r in self.strf.records if r.sid != self.current_id]
            self.current_id = None
            self.mark_dirty(True)
            self.populate()

        # ----------------------------------------------------------- audios
        def _audio_label(self, idx):
            if idx is None or idx < 0:
                return "‹ninguno›"
            return "%03d  %s" % (idx, self.strf.audio_name(idx))

        def _audio_index(self, label):
            if not label or label.startswith("‹"):
                return NO_VALUE
            try:
                return int(label.split()[0])
            except (ValueError, IndexError):
                return NO_VALUE

        def _reload_audio_combo(self):
            vals = ["‹ninguno›"] + [self._audio_label(i) for i in range(len(self.strf.audio_names))]
            self.cb_audio["values"] = vals

        def populate_audio(self):
            self.atree.delete(*self.atree.get_children())
            if not self.strf:
                return
            usage = self.strf.audio_usage()
            for i, name in enumerate(self.strf.audio_names):
                self.atree.insert("", "end", iid=str(i), values=(i, name, usage[i]))

        def rename_audio(self):
            sel = self.atree.selection()
            if not (self.strf and sel):
                return
            idx = int(sel[0])
            val = simpledialog.askstring("Renombrar audio",
                                         "Nombre nuevo (máx. %d caracteres, solo ASCII):" % AUDIO_NAME_MAX,
                                         initialvalue=self.strf.audio_names[idx], parent=self)
            if val is None:
                return
            val = val.strip()
            if len(val) > AUDIO_NAME_MAX:
                messagebox.showwarning("Demasiado largo",
                                       "Máximo %d caracteres." % AUDIO_NAME_MAX)
                return
            self.strf.audio_names[idx] = val
            self.mark_dirty(True)
            self.populate_audio()
            self._reload_audio_combo()
            self.populate()

        def add_audio(self):
            if not self.strf:
                return
            val = simpledialog.askstring("Añadir audio",
                                         "Nombre (máx. %d caracteres):" % AUDIO_NAME_MAX, parent=self)
            if not val:
                return
            val = val.strip()[:AUDIO_NAME_MAX]
            self.strf.audio_names.append(val)
            self.mark_dirty(True)
            self.populate_audio()
            self._reload_audio_combo()

        # ---------------------------------------------------------- cabecera
        def apply_header(self):
            if not self.strf:
                return
            try:
                self.strf.magic = int(self.v_magic.get())
                self.strf.version = int(self.v_version.get())
            except ValueError:
                messagebox.showwarning("Valor inválido", "magic y version deben ser números enteros.")
                return
            self.mark_dirty(True)
            self.populate_info()

        def populate_info(self):
            if not self.strf:
                return
            s = self.strf.stats()
            data = self.strf.to_bytes()
            magic, version, count, tbl, txt, aud = struct.unpack_from("<6I", data, 0)
            lines = [
                "ARCHIVO         : %s" % (self.strf.path or "(sin guardar)"),
                "",
                "CABECERA TAL COMO SE ESCRIBIRA",
                "  magic         : %d" % magic,
                "  version       : %d" % version,
                "  num_registros : %d" % count,
                "  offset tabla  : %d" % tbl,
                "  offset textos : %d" % txt,
                "  offset audios : %d" % aud,
                "  tamano total  : %d bytes" % len(data),
                "",
                "CONTENIDO",
                "  entradas          : %d" % s["entradas"],
                "  con texto         : %d" % s["con_texto"],
                "  sin texto (-1)    : %d" % s["sin_texto"],
                "  con audio         : %d" % s["con_audio"],
                "  nombres de audio  : %d" % s["audios"],
                "  caracteres totales: %d" % s["caracteres"],
                "",
                "NOTA: los offsets y tamanos se recalculan solos. El texto puede",
                "ser mas largo o mas corto que el original sin ningun problema.",
            ]
            self.info.configure(state="normal")
            self.info.delete("1.0", "end")
            self.info.insert("1.0", "\n".join(lines))
            self.info.configure(state="disabled")

        # -------------------------------------------------------------- varios
        def about(self):
            messagebox.showinfo("Acerca de",
                                "%s v%s\n\nEditor de archivos .str de MDK2.\n"
                                "Lee y reescribe el formato completo: textos UTF-16, IDs,\n"
                                "referencias de audio y tabla de nombres de clips.\n\n"
                                "Los offsets se recalculan al guardar, asi que el texto\n"
                                "traducido puede tener cualquier longitud." % (APP_NAME, APP_VER))

        def on_close(self):
            if self.dirty:
                r = messagebox.askyesnocancel("Salir", "Hay cambios sin guardar. ¿Guardar antes de salir?")
                if r is None:
                    return
                if r:
                    self.save_file()
                    if self.dirty:
                        return
            self.destroy()

    app = App()
    if initial:
        app.load_path(initial)
    if initial_ref:
        try:
            app.reference = StrFile.load(initial_ref)
            app.populate()
        except Exception:
            pass
    app.mainloop()


# =============================================================================
#  CLI
# =============================================================================

def cmd_info(path):
    s = StrFile.load(path)
    st = s.stats()
    print("Archivo : %s" % path)
    print("magic=%d  version=%d" % (s.magic, s.version))
    for k, v in st.items():
        print("  %-18s %s" % (k, v))
    print("\nPrimeras 10 entradas:")
    for r in sorted(s.records, key=lambda r: r.sid)[:10]:
        print("  id=%-4d audio=%-16s texto=%r" % (r.sid, s.audio_name(r.audio) or "-", r.text))


def cmd_verify(path):
    with open(path, "rb") as fh:
        original = fh.read()
    rebuilt = StrFile.from_bytes(original).to_bytes()
    ok = rebuilt == original
    print("Round-trip %s  (%d bytes originales, %d reconstruidos)"
          % ("IDENTICO ✓" if ok else "DIFERENTE ✗", len(original), len(rebuilt)))
    return 0 if ok else 1


def main(argv):
    if len(argv) >= 2 and argv[1] in ("info", "export", "import", "verify"):
        cmd = argv[1]
        try:
            if cmd == "info":
                cmd_info(argv[2])
            elif cmd == "verify":
                return cmd_verify(argv[2])
            elif cmd == "export":
                s = StrFile.load(argv[2])
                ref = StrFile.load(argv[4]) if len(argv) > 4 else None
                export_csv(s, argv[3], ref)
                print("CSV escrito en %s" % argv[3])
            elif cmd == "import":
                s = StrFile.load(argv[2])
                applied, skipped = import_csv(s, argv[3])
                n = s.save(argv[4])
                print("Aplicadas %d filas (%d ignoradas). Escrito %s (%d bytes)."
                      % (applied, len(skipped), argv[4], n))
        except IndexError:
            print(__doc__)
            return 2
        except StrError as ex:
            print("Error: %s" % ex)
            return 1
        return 0

    initial = argv[1] if len(argv) > 1 else None
    initial_ref = argv[2] if len(argv) > 2 else None
    try:
        run_gui(initial, initial_ref)
    except ImportError:
        print("tkinter no esta disponible. Usa el modo consola:\n" + __doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
