#!/usr/bin/env python3
"""
Test tipo autoescuela: carga bancos de preguntas desde ficheros .txt.

Uso:
    python test_app.py [fichero.txt ...]          abre la aplicación
    python test_app.py --check fichero.txt [...]  valida ficheros sin abrir ventana
    python test_app.py --zoom 1.3                 letra más grande

Al arrancar se cargan todos los .txt de la carpeta preguntas/ que hay junto a
este script. El formato de los ficheros está explicado en README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
QUESTIONS_DIR = APP_DIR / "preguntas"
FAILS_DIR = APP_DIR / "fallos"
# Cada asignatura es una subcarpeta de preguntas/ y tiene sus propios fallos en fallos/<asignatura>/.
# Los .txt sueltos directamente en preguntas/ forman la asignatura «Sin asignatura».
NO_SUBJECT = "Sin asignatura"
FORBIDDEN_IN_NAMES = '<>:"/\\|?*'


def list_subjects() -> list[str]:
    if not QUESTIONS_DIR.is_dir():
        return []
    subjects = sorted((p.name for p in QUESTIONS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=str.lower)
    if any(QUESTIONS_DIR.glob("*.txt")):
        subjects.append(NO_SUBJECT)
    return subjects


def subject_dir(subject: str) -> Path:
    return QUESTIONS_DIR if subject == NO_SUBJECT else QUESTIONS_DIR / subject


def subject_files(subject: str) -> list[Path]:
    folder = subject_dir(subject)
    return sorted(folder.glob("*.txt")) if folder.is_dir() else []


def fails_file(subject: str) -> Path:
    return FAILS_DIR / subject / "fallos.json"

# ═══════════════════════════════ Formato ════════════════════════════════

FIELD_RE = re.compile(r"^(P|R|E|T)\s*:\s?(.*)$")
OPTION_RE = re.compile(r"^([A-Za-z])\)\s?(.*)$")
DIRECTIVE_RE = re.compile(r"^@(\w+)\s*[:=]?\s*(.*)$")
SEPARATOR_RE = re.compile(r"^-{3,}$")
# Opciones que se refieren a otras: si aparecen, no se barajan las opciones.
REFERENTIAL_RE = re.compile(
    r"anteriores|lo anterior|\bninguna (es|de)\b|\btodas (las respuestas|las opciones|son)\b"
    r"|\blas (tres|dos|otras)\b|\bambas\b|\bnone of\b|\ball of\b|\bboth\b|\babove\b"
    r"|\b[a-h]\)?\s*,\s*[a-h]\b(?!')|\b[a-h]\)?\s+(y|e|and)\s+[a-h]\)?(?!\w)",
    re.IGNORECASE,
)


def quotas_for(reparto, n):
    """Ajusta el reparto del fichero (p. ej. 4+4+5+2) a n preguntas, manteniendo la proporción."""
    total = sum(reparto.values())
    if total == 0:
        return {}
    exact = {topic: count * n / total for topic, count in reparto.items()}
    quotas = {topic: int(value) for topic, value in exact.items()}
    left = n - sum(quotas.values())
    for topic in sorted(exact, key=lambda t: exact[t] - quotas[t], reverse=True)[:left]:
        quotas[topic] += 1
    return quotas


def sample_by_topic(pool, n, reparto=None):
    """Elige n preguntas repartidas entre los temas, como en un examen real.

    Si el banco trae @reparto, cada tema aporta las preguntas que indica (el nombre del
    reparto vale como inicio del tema: «Tema 1» encaja con «Tema 1 - Introducción»).
    Sin reparto, se reparten por igual. Si un tema se queda sin preguntas, las que
    faltan salen de los demás.
    """
    by_topic = {}
    for q in pool:
        by_topic.setdefault(q.topic, []).append(q)
    for questions in by_topic.values():
        random.shuffle(questions)

    chosen = []
    for prefix, quota in quotas_for(reparto or {}, n).items():
        matching = [q for topic, qs in by_topic.items() if topic.startswith(prefix) for q in qs]
        for q in matching[:quota]:
            chosen.append(q)
            by_topic[q.topic].remove(q)

    topics = list(by_topic)
    while len(chosen) < n and topics:
        random.shuffle(topics)
        for topic in list(topics):
            if len(chosen) == n:
                break
            if by_topic[topic]:
                chosen.append(by_topic[topic].pop())
            else:
                topics.remove(topic)

    random.shuffle(chosen)
    return chosen


@dataclass
class Question:
    text: str
    options: list[str]
    correct: frozenset[int]
    explanation: str = ""
    topic: str = ""
    source: str = ""
    line: int = 0

    @property
    def multiple(self) -> bool:
        return len(self.correct) > 1

    @property
    def key(self) -> str:
        raw = self.text.strip() + "\x00" + "\x00".join(o.strip() for o in self.options)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Bank:
    path: Path
    title: str
    settings: dict
    questions: list[Question]
    warnings: list[str]


def _join(lines: list[str]) -> str:
    lines = list(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _set_directive(settings: dict, name: str, value: str, where: str, warnings: list[str]) -> None:
    name = _strip_accents(name.lower())
    value = value.strip()
    try:
        if name == "titulo":
            settings["titulo"] = value
        elif name == "tiempo":
            settings["tiempo"] = max(0, int(value))
        elif name == "preguntas":
            settings["preguntas"] = max(1, int(value))
        elif name == "penalizacion":
            settings["penalizacion"] = max(0.0, float(value.replace(",", ".")))
        elif name == "reparto":
            # @reparto: Tema 1=4; Tema 2=4; Tema 3=5; Tema 4=2
            reparto = {}
            for part in value.split(";"):
                topic, _, count = part.partition("=")
                reparto[topic.strip()] = max(0, int(count))
            settings["reparto"] = reparto
        else:
            warnings.append(f"{where}: directiva desconocida @{name}")
    except ValueError:
        warnings.append(f"{where}: valor no válido para @{name}: {value!r}")


def _parse_answer(raw: str, letters: list[str]) -> tuple[set[int], str | None]:
    cleaned = re.sub(r"\b(y|e|and)\b", " ", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\s,;/+\-().]", "", cleaned).lower()
    if not cleaned:
        return set(), "falta la respuesta en R:"
    result = set()
    for ch in cleaned:
        if ch not in letters:
            return set(), f"la respuesta {raw.strip()!r} no corresponde a ninguna opción"
        result.add(letters.index(ch))
    return result, None


def parse_text(text: str, source: str = "") -> tuple[dict, list[Question], list[str]]:
    """Devuelve (directivas, preguntas, avisos). Las preguntas mal formadas se saltan."""
    settings: dict = {}
    questions: list[Question] = []
    warnings: list[str] = []
    name = Path(source).name if source else "<texto>"
    sticky_topic = ""
    cur: dict | None = None
    state: str | None = None  # None, "P", "O", "R", "E"
    in_fence = False

    def finish() -> None:
        nonlocal cur, state
        if cur is not None:
            q = _build(cur, source, name, warnings)
            if q:
                questions.append(q)
        cur, state = None, None

    def append(line: str, n: int) -> None:
        if state == "P":
            cur["text"].append(line)
        elif state == "O":
            cur["options"][-1][1].append(line)
        elif state == "E":
            cur["expl"].append(line)
        elif line.strip():
            warnings.append(f"{name}:{n}: línea ignorada: {line.strip()[:50]!r}")

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if in_fence or stripped.startswith("```"):
            if stripped.startswith("```"):
                in_fence = not in_fence
            append(line, n)
            continue
        if SEPARATOR_RE.match(stripped):
            finish()
            continue
        if stripped.startswith("%"):
            continue
        if not stripped:
            if state in ("P", "O", "E"):
                append("", n)
            continue
        left = line.lstrip()

        if cur is None:
            m = DIRECTIVE_RE.match(stripped)
            if m:
                _set_directive(settings, m.group(1), m.group(2), f"{name}:{n}", warnings)
                continue

        m = FIELD_RE.match(left)
        key = m.group(1) if m else None
        # Dentro de la explicación solo P: (pregunta nueva) y T: cortan el texto.
        if key and (state != "E" or key in ("P", "T")):
            value = m.group(2)
            if key == "P":
                finish()
                cur = {"line": n, "text": [value] if value.strip() else [], "options": [],
                       "answer": None, "expl": [], "topic": sticky_topic}
                state = "P"
            elif key == "T":
                sticky_topic = value.strip()
                if cur is not None and state in ("P", "O"):
                    cur["topic"] = sticky_topic
            elif cur is None:
                warnings.append(f"{name}:{n}: {key}: sin una P: antes")
            elif key == "R":
                if cur["answer"] is not None:
                    warnings.append(f"{name}:{n}: R: repetida, se usa la última")
                cur["answer"] = (value, n)
                state = "R"
            elif key == "E":
                cur["expl"] = [value] if value.strip() else []
                state = "E"
            continue

        if state in ("P", "O"):
            mo = OPTION_RE.match(left)
            if mo:
                cur["options"].append([mo.group(1).lower(), [mo.group(2)]])
                state = "O"
                continue
        append(line, n)

    if in_fence:
        warnings.append(f"{name}: hay un bloque ``` sin cerrar")
    finish()
    return settings, questions, warnings


def _build(cur: dict, source: str, name: str, warnings: list[str]) -> Question | None:
    where = f"{name}:{cur['line']}"
    text = _join(cur["text"])
    options = [(letter, _join(lines)) for letter, lines in cur["options"]]
    letters = [letter for letter, _ in options]
    problem = None
    if not text:
        problem = "pregunta sin texto"
    elif len(options) < 2:
        problem = "hacen falta al menos 2 opciones (a) b) ...)"
    elif len(set(letters)) != len(letters):
        problem = "hay letras de opción repetidas"
    elif any(not t for _, t in options):
        problem = "hay una opción vacía"
    elif cur["answer"] is None:
        problem = "falta la línea R: con la respuesta"
    if problem:
        warnings.append(f"{where}: {problem}; se salta la pregunta")
        return None
    correct, err = _parse_answer(cur["answer"][0], letters)
    if err:
        warnings.append(f"{name}:{cur['answer'][1]}: {err}; se salta la pregunta")
        return None
    return Question(text=text, options=[t for _, t in options], correct=frozenset(correct),
                    explanation=_join(cur["expl"]), topic=cur["topic"], source=source,
                    line=cur["line"])


def load_bank(path: Path) -> Bank:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    settings, questions, warnings = parse_text(text, str(path))
    return Bank(path=path, title=settings.get("titulo") or path.stem, settings=settings,
                questions=questions, warnings=warnings)


# ═══════════════════════════ Fallos guardados ═══════════════════════════

class FailStore:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            self.data = {}
        # Historial de todo lo fallado alguna vez. A diferencia de fallos.json, no se borra
        # al acertar, para poder repasarlo después (por ejemplo, pasárselo a Claude).
        self.history_path = path.with_name("historial_fallos.json")
        try:
            self.history: dict = json.loads(self.history_path.read_text("utf-8"))
        except (OSError, ValueError):
            self.history = {}

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def __len__(self) -> int:
        return len(self.data)

    def record(self, q: Question, ok: bool, save: bool = True) -> None:
        if ok:
            self.data.pop(q.key, None)
        else:
            e = self.data.setdefault(q.key, {"fuente": Path(q.source).name,
                                             "pregunta": q.text[:100], "veces": 0})
            e["veces"] += 1
            e["ultimo"] = datetime.now().isoformat(timespec="seconds")
            h = self.history.setdefault(q.key, {"fuente": Path(q.source).name, "tema": q.topic,
                                                "pregunta": q.text, "veces": 0})
            h["veces"] += 1
            h["ultimo"] = e["ultimo"]
        if save:
            self.save()

    def clear(self) -> None:
        self.data = {}
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for path, data in ((self.path, self.data), (self.history_path, self.history)):
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
                tmp.replace(path)
        except OSError as e:
            print(f"No se pudieron guardar los fallos: {e}", file=sys.stderr)


# ═══════════════════════════════ Sesiones ═══════════════════════════════

@dataclass
class Item:
    q: Question
    order: list[int]  # posición mostrada -> índice original de la opción
    selected: set[int] = field(default_factory=set)  # posiciones mostradas
    checked: bool = False  # en práctica: ya corregida

    def status(self) -> str:
        if not self.selected:
            return "blank"
        return "ok" if {self.order[i] for i in self.selected} == set(self.q.correct) else "ko"

    def correct_letters(self) -> str:
        return ", ".join(chr(97 + pos) for pos, orig in enumerate(self.order) if orig in self.q.correct)


def make_items(questions: list[Question], shuffle_options: bool) -> list[Item]:
    items = []
    for q in questions:
        order = list(range(len(q.options)))
        if shuffle_options and not any(REFERENTIAL_RE.search(o) for o in q.options):
            random.shuffle(order)
        items.append(Item(q, order))
    return items


@dataclass
class Session:
    mode: str  # "practica", "examen" o "repaso"
    items: list[Item]
    time_limit: int  # segundos, 0 = sin límite
    penalty: float  # cada N fallos restan un acierto, 0 = sin penalización
    title: str
    shuffle_questions: bool = True
    shuffle_options: bool = True
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None

    @property
    def is_exam(self) -> bool:
        return self.mode == "examen"

    def scored(self) -> list[Item]:
        return self.items if self.is_exam else [i for i in self.items if i.checked]

    def score(self) -> dict:
        items = self.scored()
        st = [i.status() for i in items]
        ok, ko, blank = st.count("ok"), st.count("ko"), st.count("blank")
        pen = ko / self.penalty if self.penalty else 0.0
        net = ok - pen
        grade = max(0.0, 10 * net / len(items)) if items else 0.0
        elapsed = (self.finished or time.monotonic()) - self.started
        return dict(total=len(items), ok=ok, ko=ko, blank=blank, penalty=pen, net=net,
                    grade=grade, elapsed=elapsed)

    def again(self, questions: list[Question] | None = None, mode: str | None = None) -> Session:
        qs = list(questions if questions is not None else [i.q for i in self.items])
        if self.shuffle_questions:
            random.shuffle(qs)
        mode = mode or self.mode
        return Session(mode=mode, items=make_items(qs, self.shuffle_options),
                       time_limit=self.time_limit if mode == self.mode else 0,
                       penalty=self.penalty, title=self.title,
                       shuffle_questions=self.shuffle_questions,
                       shuffle_options=self.shuffle_options)


LETTER_REF_RE = re.compile(r"\b([a-z])\)")


def remap_letters(text: str, item: Item) -> str:
    """Traduce las referencias «a)», «b)»... de un texto al orden en que se muestran las opciones."""
    if item.order == sorted(item.order):
        return text
    n = len(item.order)

    def sub(m):
        orig = ord(m.group(1)) - 97
        if orig >= n:
            return m.group(0)
        return chr(97 + item.order.index(orig)) + ")"

    return LETTER_REF_RE.sub(sub, text)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ═══════════════════════════════ Interfaz ═══════════════════════════════

def run_gui(files: list[str], zoom: float, mainloop: bool = True):
    import tkinter as tk
    from tkinter import filedialog, simpledialog, ttk
    import tkinter.font as tkfont

    SETTINGS_FILE = APP_DIR / "ajustes.json"

    # Papel cálido y tinta; un único acento. Verde/rojo solo para acierto/fallo.
    PALETTES = {
        "light": dict(
            bg="#F4F2ED", surface="#FFFFFF", surface2="#ECE9E2", border="#DEDAD1",
            border_strong="#C5BFB3", text="#1C2024", muted="#625E57", faint="#9B968C",
            accent="#2C58B0", accent_hover="#21468F", accent_text="#FFFFFF", accent_soft="#E4EBF8",
            ok="#2C7A4D", ok_soft="#E1F0E6", on_ok="#FFFFFF",
            ko="#B9382A", ko_soft="#F9E5E1", on_ko="#FFFFFF",
            blank_soft="#E8E5DE", code_bg="#F0EDE6", select="#D5E0F5"),
        "dark": dict(
            bg="#14161A", surface="#1D2025", surface2="#262A30", border="#30353D",
            border_strong="#454C57", text="#E8E6E1", muted="#A39F96", faint="#6E6A63",
            accent="#82A6F0", accent_hover="#A0BCF5", accent_text="#0E1320", accent_soft="#1F2C47",
            ok="#5CC08A", ok_soft="#183025", on_ok="#0B1A12",
            ko="#EE8170", ko_soft="#3A211E", on_ko="#24100C",
            blank_soft="#2A2D33", code_bg="#252930", select="#2B3D63"),
    }

    def load_settings() -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(data: dict) -> None:
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=1), "utf-8")
        except OSError:
            pass

    def int_of(text: str, default: int, lo: int, hi: int = 10 ** 6) -> int:
        try:
            return min(hi, max(lo, int(float(str(text).replace(",", ".")))))
        except ValueError:
            return default

    def num(x: float, decimals: int = 2) -> str:
        return f"{x:.{decimals}f}".replace(".", ",").replace("-", "−")

    def clean_option(text: str) -> str:
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        return "\n".join(lines).replace("`", "")

    # ───────────────────────────── piezas ─────────────────────────────
    class Btn(tk.Label):
        """Botón plano. kind: primary, secondary, ghost, danger, danger_solid."""
        SIZES = {"sm": ("small_b", 10, 5), "md": ("small_b", 15, 8), "lg": ("body_b", 24, 11)}

        def __init__(self, parent, app, text, command=None, kind="secondary", size="md", bg=None):
            self.app, self.kind, self.command = app, kind, command
            self.parent_bg = bg or app.P["bg"]
            self.enabled = True
            font, padx, pady = self.SIZES[size]
            super().__init__(parent, text=text, font=app.F[font], padx=padx, pady=pady, bd=0,
                             highlightthickness=1, takefocus=0)
            self.bind("<Enter>", lambda *_: self.paint(True))
            self.bind("<Leave>", lambda *_: self.paint(False))
            self.bind("<ButtonRelease-1>", self._release)
            self.paint(False)

        def _colors(self, hover):
            P, k = self.app.P, self.kind
            if not self.enabled:
                if k in ("primary", "danger_solid"):
                    return P["surface2"], P["faint"], P["surface2"]
                if k == "secondary":
                    return P["surface"], P["faint"], P["border"]
                return self.parent_bg, P["faint"], self.parent_bg
            if k == "primary":
                c = P["accent_hover"] if hover else P["accent"]
                return c, P["accent_text"], c
            if k == "danger_solid":
                return P["ko"], P["on_ko"], P["ko"]
            if k == "secondary":
                return (P["surface2"] if hover else P["surface"], P["text"],
                        P["border_strong"] if hover else P["border"])
            if k == "danger":
                c = P["ko_soft"] if hover else self.parent_bg
                return c, P["ko"], c
            c = P["surface2"] if hover else self.parent_bg
            return c, P["text"] if hover else P["muted"], c

        def paint(self, hover=False):
            bg, fg, bd = self._colors(hover and self.enabled)
            self.configure(bg=bg, fg=fg, highlightbackground=bd, highlightcolor=bd,
                           cursor="hand2" if self.enabled else "arrow")

        def _release(self, e=None):
            if e is None or not hasattr(e, "x"):
                return
            if self.enabled and self.command and 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
                self.command()

        def set_enabled(self, on: bool):
            self.enabled = on
            self.paint(False)

    class Check(tk.Frame):
        """Casilla cuadrada de 18 px."""
        def __init__(self, parent, app, checked, bg):
            super().__init__(parent, width=18, height=18, bg=bg)
            self.app = app
            self.pack_propagate(False)
            self.lbl = tk.Label(self, font=app.F["tiny_b"], bd=0, highlightthickness=2)
            self.lbl.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.set(checked)

        def set(self, checked):
            P = self.app.P
            if checked:
                self.lbl.configure(text="✓", bg=P["accent"], fg=P["accent_text"], highlightbackground=P["accent"])
            else:
                self.lbl.configure(text="", bg=P["surface"], fg=P["surface"], highlightbackground=P["border_strong"])

    def make_clickable(widgets, command, on_enter=None, on_leave=None):
        for w in widgets:
            w.bind("<Button-1>", lambda *_: command())
            w.configure(cursor="hand2")
            if on_enter:
                w.bind("<Enter>", lambda *_: on_enter())
                w.bind("<Leave>", lambda *_: on_leave())

    class RichText(tk.Frame):
        """Bloque de texto de solo lectura: párrafos, bloques ``` en monoespaciada y líneas «Fuente:».
        Hecho con Labels para que Tk calcule su tamaño de forma exacta."""
        def __init__(self, parent, app, font, bg, fg=None, sources=False):
            super().__init__(parent, bg=bg)
            self.app, self.font, self.bg = app, font, bg
            self.fg = fg or app.P["text"]
            self.sources = sources
            self.wrapping: list[tk.Label] = []
            self.bind("<Configure>", lambda *_: self._wrap(self.winfo_width()))

        def _blocks(self, text):
            blocks, para, code, in_code = [], [], [], False

            def flush_para():
                if para:
                    blocks.append(("para", "\n".join(para).strip("\n")))
                    para.clear()

            for line in text.split("\n"):
                st = line.strip()
                if st.startswith("```"):
                    if in_code:
                        blocks.append(("code", "\n".join(code)))
                        code.clear()
                    else:
                        flush_para()
                    in_code = not in_code
                elif in_code:
                    code.append(line)
                elif self.sources and st.startswith("Fuente:"):
                    flush_para()
                    blocks.append(("source", st))
                else:
                    para.append(line.replace("`", ""))
            if in_code and code:
                blocks.append(("code", "\n".join(code)))
            flush_para()
            return [(k, t) for k, t in blocks if t.strip()]

        def set(self, text):
            P, F = self.app.P, self.app.F
            for w in self.winfo_children():
                w.destroy()
            self.wrapping = []
            for i, (kind, body) in enumerate(self._blocks(text)):
                top = 0 if i == 0 else 8
                if kind == "code":
                    lbl = tk.Label(self, text=body, font=F["mono"], bg=P["code_bg"], fg=P["text"],
                                   justify="left", anchor="w", padx=14, pady=9)
                    lbl.pack(fill="x", pady=(top, 0))
                elif kind == "source":
                    lbl = tk.Label(self, text=body, font=F["tiny"], bg=self.bg, fg=P["faint"],
                                   justify="left", anchor="w")
                    lbl.pack(fill="x", pady=(10, 0))
                else:
                    lbl = tk.Label(self, text=body, font=self.font, bg=self.bg, fg=self.fg,
                                   justify="left", anchor="w")
                    lbl.pack(fill="x", pady=(top, 0))
                    self.wrapping.append(lbl)
            self._wrap(self.winfo_width())

        def _wrap(self, width):
            for lbl in self.wrapping:
                lbl.configure(wraplength=max(200, width - 4))

    class Scroller(tk.Frame):
        """Columna desplazable; si maxwidth, el contenido se centra con ese ancho máximo."""
        def __init__(self, parent, app, bg, maxwidth=None, padx=0, pady=0):
            super().__init__(parent, bg=bg)
            self.maxwidth, self.padx, self.pady = maxwidth, padx, pady
            self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=36)
            self.canvas._scroller = self
            self.canvas.pack(fill="both", expand=True)
            self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                    style="App.Vertical.TScrollbar")
            self.canvas.configure(yscrollcommand=self._on_scroll)
            self.inner = tk.Frame(self.canvas, bg=bg)
            self.win = self.canvas.create_window(0, pady, window=self.inner, anchor="n")
            self.inner.bind("<Configure>", lambda *_: self._region())
            self.canvas.bind("<Configure>", self._resize)

        def _on_scroll(self, lo, hi):
            if float(lo) <= 0.0 and float(hi) >= 1.0:
                self.sb.place_forget()
            else:
                self.sb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
            self.sb.set(lo, hi)

        def _resize(self, *_):
            cw = self.canvas.winfo_width()
            width = cw - 2 * self.padx
            if self.maxwidth:
                width = min(width, self.maxwidth)
            self.canvas.coords(self.win, cw / 2, self.pady)
            self.canvas.itemconfigure(self.win, width=max(200, width))
            self._region()

        def _region(self):
            h = self.inner.winfo_reqheight() + 2 * self.pady
            self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), h))

        def top(self):
            self.canvas.yview_moveto(0)

        def see(self, widget):
            self.update_idletasks()
            total = self.inner.winfo_height() + 2 * self.pady
            view = self.canvas.winfo_height()
            if total <= view:
                return
            top = widget.winfo_rooty() - self.inner.winfo_rooty() + self.pady
            bottom = top + widget.winfo_height()
            y0 = self.canvas.canvasy(0)
            if bottom + 16 > y0 + view:
                target = top - 16 if widget.winfo_height() + 32 > view else bottom - view + 16
                self.canvas.yview_moveto(max(0, target) / total)

        def scroll(self, units):
            lo, hi = self.canvas.yview()
            if lo > 0 or hi < 1:
                self.canvas.yview_scroll(units, "units")

    class Bar(tk.Canvas):
        """Barra horizontal de segmentos [(fracción, color)] sobre una pista."""
        def __init__(self, parent, app, segments, height=8, bg=None, track=None):
            super().__init__(parent, height=height, bg=bg or app.P["bg"], highlightthickness=0, bd=0)
            self.segments, self.track = segments, track or app.P["surface2"]
            self.bind("<Configure>", lambda *_: self.draw())

        def set(self, segments):
            self.segments = segments
            self.draw()

        def draw(self):
            self.delete("all")
            w, h = self.winfo_width(), int(self.cget("height"))
            self.create_rectangle(0, 0, w, h, fill=self.track, width=0)
            x = 0.0
            for frac, color in self.segments:
                if frac <= 0:
                    continue
                x2 = x + frac * w
                self.create_rectangle(round(x), 0, round(x2), h, fill=color, width=0)
                x = x2

    def section_label(parent, app, text, bg=None):
        return tk.Label(parent, text=text.upper(), font=app.F["tiny_b"], bg=bg or app.P["bg"],
                        fg=app.P["faint"], anchor="w")

    def card(parent, app, bg_key="surface", border_key="border", thick=1):
        P = app.P
        return tk.Frame(parent, bg=P[bg_key], highlightthickness=thick,
                        highlightbackground=P[border_key], highlightcolor=P[border_key])

    def dialog(app, title, message, ok="Aceptar", cancel=None, danger=False) -> bool:
        P, F = app.P, app.F
        top = tk.Toplevel(app)
        top.title(title)
        top.configure(bg=P["surface"])
        top.transient(app)
        top.resizable(False, False)
        result = {"value": False}
        body = tk.Frame(top, bg=P["surface"], padx=30, pady=24)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=title, font=F["h2"], bg=P["surface"], fg=P["text"], anchor="w").pack(fill="x")
        tk.Label(body, text=message, font=F["body"], bg=P["surface"], fg=P["muted"], justify="left",
                 wraplength=440, anchor="w").pack(fill="x", pady=(8, 22))
        row = tk.Frame(body, bg=P["surface"])
        row.pack(fill="x")

        def close(value):
            result["value"] = value
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()

        Btn(row, app, ok, lambda: close(True), kind="danger_solid" if danger else "primary",
            bg=P["surface"]).pack(side="right")
        if cancel:
            Btn(row, app, cancel, lambda: close(False), kind="secondary", bg=P["surface"]).pack(
                side="right", padx=(0, 10))
        top.bind("<Return>", lambda *_: close(True))
        top.bind("<Escape>", lambda *_: close(not cancel))
        top.protocol("WM_DELETE_WINDOW", lambda: close(False))
        top.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - top.winfo_reqwidth()) // 2
        y = app.winfo_rooty() + (app.winfo_height() - top.winfo_reqheight()) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            top.wait_visibility()
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_set()
        app.wait_window(top)
        return result["value"]

    # ───────────────────────────── App ─────────────────────────────
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Test tipo autoescuela")
            self.settings = load_settings()
            self.theme = self.settings.get("theme", "light")
            if self.theme not in PALETTES:
                self.theme = "light"
            self.P = PALETTES[self.theme]
            # que quepa en pantallas pequeñas o con el zoom de Windows (p. ej. 1536x864)
            width = min(1180, self.winfo_screenwidth() - 40)
            height = min(840, self.winfo_screenheight() - 90)
            self.geometry(f"{width}x{height}")
            self.minsize(min(1000, width), min(700, height))
            self.style = ttk.Style(self)
            if "clam" in self.style.theme_names():
                self.style.theme_use("clam")
            self._fonts(zoom)
            self.apply_theme()
            self.config_state: dict = {}
            subjects = list_subjects()
            subject = self.settings.get("subject")
            if subject not in subjects:
                subject = subjects[0] if subjects else NO_SUBJECT
            self.load_subject(subject, extra=[Path(f) for f in files])
            self.screen = None
            self.bind_all("<Key>", self._on_key)
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.bind_all(seq, self._on_wheel)
            self.show_start()

        def _fonts(self, z: float) -> None:
            fams = set(tkfont.families(self))
            ui = next((f for f in ("Lato", "Segoe UI", "Helvetica Neue", "Noto Sans", "Ubuntu", "DejaVu Sans")
                       if f in fams), "TkDefaultFont")
            mono = next((f for f in ("Ubuntu Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono")
                         if f in fams), "TkFixedFont")

            def mk(size, weight="normal", family=ui):
                return tkfont.Font(self, family=family, size=round(size * z), weight=weight)

            self.F = dict(display=mk(46, "bold"), h1=mk(22, "bold"), h2=mk(16, "bold"), h3=mk(13, "bold"),
                          question=mk(16), option=mk(13), body=mk(12), body_b=mk(12, "bold"),
                          small=mk(11), small_b=mk(11, "bold"), tiny=mk(10), tiny_b=mk(10, "bold"),
                          mono=mk(13, family=mono), letter=mk(13, "bold"))
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(family=ui, size=round(11 * z))
            tkfont.nametofont("TkFixedFont").configure(family=mono, size=round(12 * z))

        def apply_theme(self) -> None:
            P, st = self.P, self.style
            self.configure(bg=P["bg"])
            st.layout("App.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
                "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
                "sticky": "ns"})])
            st.configure("App.Vertical.TScrollbar", troughcolor=P["bg"], background=P["border_strong"],
                         bordercolor=P["bg"], lightcolor=P["border_strong"], darkcolor=P["border_strong"],
                         gripcount=0, arrowsize=9)
            st.map("App.Vertical.TScrollbar", background=[("active", P["faint"])])
            st.configure("App.TCombobox", fieldbackground=P["surface"], background=P["surface"],
                         foreground=P["text"], arrowcolor=P["muted"], bordercolor=P["border"],
                         lightcolor=P["surface"], darkcolor=P["surface"], selectbackground=P["surface"],
                         selectforeground=P["text"], padding=(10, 7), arrowsize=17, insertcolor=P["text"])
            st.map("App.TCombobox", fieldbackground=[("readonly", P["surface"])],
                   foreground=[("readonly", P["text"])], background=[("active", P["surface2"])],
                   bordercolor=[("focus", P["accent"]), ("active", P["border_strong"])],
                   arrowcolor=[("active", P["text"])])
            self.option_add("*TCombobox*Listbox.background", P["surface"])
            self.option_add("*TCombobox*Listbox.foreground", P["text"])
            self.option_add("*TCombobox*Listbox.selectBackground", P["accent"])
            self.option_add("*TCombobox*Listbox.selectForeground", P["accent_text"])
            self.option_add("*TCombobox*Listbox.font", self.F["body"])
            self.option_add("*TCombobox*Listbox.borderWidth", 0)

        def toggle_theme(self) -> None:
            self.theme = "dark" if self.theme == "light" else "light"
            self.P = PALETTES[self.theme]
            self.settings["theme"] = self.theme
            save_settings(self.settings)
            self.apply_theme()
            if isinstance(self.screen, StartScreen):
                self.screen.save_state()
            self.show_start()

        def dialog(self, *args, **kw) -> bool:
            return dialog(self, *args, **kw)

        # --- asignaturas ---
        def load_subject(self, subject: str, extra: list[Path] = ()) -> None:
            """Carga los bancos y los fallos de una asignatura."""
            self.subject = subject
            self.fails = FailStore(fails_file(subject))
            self.banks: list[Bank] = []
            for p in subject_files(subject) + list(extra):
                self.add_bank(p, quiet=True)

        def change_subject(self, subject: str) -> None:
            if subject == self.subject:
                return
            # la selección de bancos y los ajustes eran de la otra asignatura: se empieza de cero,
            # así se aplican el tiempo, el número de preguntas y la penalización de sus bancos
            self.config_state = {}
            self.load_subject(subject)
            self.settings["subject"] = subject
            save_settings(self.settings)
            self.show_start()

        def new_subject(self) -> None:
            name = simpledialog.askstring("Nueva asignatura", "Nombre de la asignatura (por ejemplo, ArCO):",
                                          parent=self)
            if name is None:
                return
            name = name.strip()
            if not name or name == NO_SUBJECT or name.startswith(".") or any(c in name for c in FORBIDDEN_IN_NAMES):
                self.dialog("Nombre no válido", f"Escribe otro nombre. No puede estar vacío, empezar por un "
                                                f"punto ni llevar estos caracteres: {FORBIDDEN_IN_NAMES}")
                return
            folder = QUESTIONS_DIR / name
            if folder.exists():
                self.dialog("Ya existe", f"Ya hay una asignatura llamada «{name}».")
            else:
                folder.mkdir(parents=True)
            self.change_subject(name)

        def add_bank(self, path: Path, quiet: bool = False) -> Bank | None:
            path = path.expanduser().resolve()
            try:
                bank = load_bank(path)
            except OSError as e:
                if quiet:
                    print(f"No se pudo leer {path}: {e}", file=sys.stderr)
                else:
                    self.dialog("No se pudo leer el fichero", f"{path}\n\n{e}")
                return None
            for i, b in enumerate(self.banks):
                if b.path == path:
                    self.banks[i] = bank
                    return bank
            self.banks.append(bank)
            return bank

        def switch(self, cls, *args, **kw) -> None:
            if self.screen is not None:
                self.screen.destroy()
            self.screen = cls(self, *args, **kw)
            self.screen.pack(fill="both", expand=True)

        def show_start(self) -> None:
            self.switch(StartScreen)

        def _mine(self, e) -> bool:
            try:
                return e.widget.winfo_toplevel() is self
            except (AttributeError, KeyError, tk.TclError):
                return False

        def _on_key(self, e) -> None:
            if not self._mine(e) or isinstance(e.widget, (tk.Entry, ttk.Combobox)):
                return
            handler = getattr(self.screen, "on_key", None)
            if handler:
                handler(e)

        def _on_wheel(self, e) -> None:
            if not self._mine(e):
                return
            units = -1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1
            w = e.widget
            while w is not None:
                scroller = getattr(w, "_scroller", None)
                if scroller:
                    scroller.scroll(units)
                    return
                w = getattr(w, "master", None)

    # ────────────────────────── Pantalla inicial ──────────────────────────
    class StartScreen(tk.Frame):
        MODES = [("practica", "Práctica", "Corrige cada pregunta al momento y te enseña la explicación."),
                 ("examen", "Examen", "Con tiempo y cuadrícula de preguntas. Se corrige todo al entregar."),
                 ("repaso", "Repaso de fallos", "")]

        def __init__(self, app: App):
            P, F = app.P, app.F
            super().__init__(app, bg=P["bg"])
            self.app = app
            st = app.config_state
            all_paths = [str(b.path) for b in app.banks]
            self.sel = set(st["selected"]) & set(all_paths) if "selected" in st else set(all_paths)
            self.mode = st.get("mode", "practica")
            self.topic = tk.StringVar(value=st.get("topic", "Todos los temas"))
            self.shuffle_q = st.get("shuffle_q", True)
            self.shuffle_o = st.get("shuffle_o", True)
            self._defaults_from = st.get("defaults_from")

            # cabecera
            head = tk.Frame(self, bg=P["bg"])
            head.pack(fill="x", padx=40, pady=(30, 20))
            titles = tk.Frame(head, bg=P["bg"])
            titles.pack(side="left")
            tk.Label(titles, text="Test tipo autoescuela", font=F["h1"], bg=P["bg"], fg=P["text"]).pack(anchor="w")
            tk.Label(titles, text="Elige qué estudiar y cómo quieres practicarlo.", font=F["body"],
                     bg=P["bg"], fg=P["muted"]).pack(anchor="w", pady=(2, 0))
            Btn(head, app, "Tema oscuro" if app.theme == "light" else "Tema claro", app.toggle_theme,
                kind="secondary").pack(side="right", anchor="n")

            # asignatura: cada una tiene sus bancos (preguntas/<asignatura>/) y sus fallos
            subj = tk.Frame(self, bg=P["bg"])
            subj.pack(fill="x", padx=40, pady=(0, 18))
            tk.Label(subj, text="Asignatura", font=F["small_b"], bg=P["bg"], fg=P["muted"]).pack(side="left")
            self.subject = tk.StringVar(value=app.subject)
            subjects = list_subjects()
            if app.subject not in subjects:
                subjects.append(app.subject)
            self.subject_cb = ttk.Combobox(subj, textvariable=self.subject, values=subjects, state="readonly",
                                           style="App.TCombobox", font=F["body"], width=28)
            self.subject_cb.pack(side="left", padx=(12, 10))
            self.subject_cb.bind("<<ComboboxSelected>>",
                                 lambda *_: (self.subject_cb.selection_clear(), app.change_subject(self.subject.get())))
            Btn(subj, app, "Nueva asignatura…", app.new_subject, size="sm").pack(side="left")

            # pie (antes que el cuerpo para que quede abajo)
            foot = tk.Frame(self, bg=P["bg"])
            foot.pack(side="bottom", fill="x", padx=40, pady=(0, 22))
            self.fails_lbl = tk.Label(foot, font=F["small"], bg=P["bg"], fg=P["muted"])
            self.fails_lbl.pack(side="left")
            self.clear_btn = Btn(foot, app, "Borrar fallos guardados", self.clear_fails, kind="danger", size="sm")
            self.clear_btn.pack(side="left", padx=(10, 0))
            self.warn_btn = Btn(foot, app, "", self.show_warnings, kind="ghost", size="sm")

            body = tk.Frame(self, bg=P["bg"])
            body.pack(fill="both", expand=True, padx=40)
            body.columnconfigure(0, weight=11, uniform="c")
            body.columnconfigure(1, weight=9, uniform="c")
            body.rowconfigure(0, weight=1)

            # columna izquierda: bancos
            left = tk.Frame(body, bg=P["bg"])
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 28), pady=(0, 18))
            lhead = tk.Frame(left, bg=P["bg"])
            lhead.pack(fill="x", pady=(0, 10))
            section_label(lhead, app, "Bancos de preguntas").pack(side="left")
            self.count_lbl = tk.Label(lhead, font=F["tiny"], bg=P["bg"], fg=P["faint"])
            self.count_lbl.pack(side="right")
            actions = tk.Frame(left, bg=P["bg"])
            actions.pack(side="bottom", fill="x", pady=(12, 0))
            Btn(actions, app, "Añadir fichero…", self.add_files, size="sm").pack(side="left")
            Btn(actions, app, "Recargar", self.reload, size="sm").pack(side="left", padx=8)
            Btn(actions, app, "Todos / ninguno", self.toggle_all, kind="ghost", size="sm").pack(side="left")
            self.list = Scroller(left, app, bg=P["bg"])
            self.list.pack(fill="both", expand=True)

            # columna derecha: modo y ajustes
            right = tk.Frame(body, bg=P["bg"])
            right.grid(row=0, column=1, sticky="nsew", pady=(0, 18))

            # botón de empezar (antes que el resto para que nunca se quede sin sitio)
            go = tk.Frame(right, bg=P["bg"])
            go.pack(side="bottom", fill="x")
            self.summary = tk.Label(go, font=F["small"], bg=P["bg"], fg=P["muted"], anchor="w")
            self.summary.pack(fill="x", pady=(0, 8))
            self.start_btn = Btn(go, app, "Empezar  →", self.start, kind="primary", size="lg")
            self.start_btn.pack(fill="x")

            section_label(right, app, "Modo").pack(fill="x", pady=(0, 10))
            self.tiles = {}
            for key, title, desc in self.MODES:
                self.tiles[key] = self._mode_tile(right, key, title, desc)

            section_label(right, app, "Ajustes").pack(fill="x", pady=(18, 8))
            form = tk.Frame(right, bg=P["bg"])
            form.pack(fill="x")
            form.columnconfigure(1, weight=1)

            def label(row, text):
                tk.Label(form, text=text, font=F["small"], bg=P["bg"], fg=P["muted"], anchor="w").grid(
                    row=row, column=0, sticky="w", pady=5, padx=(0, 14))

            label(0, "Tema")
            self.topic_cb = ttk.Combobox(form, textvariable=self.topic, state="readonly",
                                         style="App.TCombobox", font=F["body"])
            self.topic_cb.grid(row=0, column=1, sticky="ew", pady=5)
            self.topic_cb.bind("<<ComboboxSelected>>", lambda *_: (self.topic_cb.selection_clear(), self.refresh()))
            label(1, "Preguntas")
            self.n_step, self.n_note = self._stepper(form, 1, st.get("n", 15), 1, 9999, 5)
            label(2, "Tiempo")
            self.min_step, _ = self._stepper(form, 2, st.get("minutes", 0), 0, 600, 5, note="min · 0 = sin límite")
            label(3, "Penalización")
            self.pen_step, _ = self._stepper(form, 3, st.get("pen", 0), 0, 20, 1,
                                             note="fallos restan un acierto · 0 = no")
            toggles = tk.Frame(form, bg=P["bg"])
            toggles.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
            self.chk_q = self._toggle(toggles, "Barajar preguntas", "shuffle_q")
            self.chk_o = self._toggle(toggles, "Barajar opciones", "shuffle_o")

            self.fill_list()
            self.on_selection(initial=True)

        # --- piezas de la pantalla ---
        def _mode_tile(self, parent, key, title, desc):
            app, P, F = self.app, self.app.P, self.app.F
            t = tk.Frame(parent, highlightthickness=2)
            t.pack(fill="x", pady=(0, 8))
            dot = tk.Label(t, font=F["body_b"], width=2)
            dot.pack(side="left", anchor="n", padx=(12, 4), pady=12)
            txt = tk.Frame(t)
            txt.pack(side="left", fill="x", expand=True, pady=10, padx=(0, 14))
            ttl = tk.Label(txt, text=title, font=F["body_b"], anchor="w")
            ttl.pack(fill="x")
            dsc = tk.Label(txt, text=desc, font=F["small"], anchor="w", justify="left")
            dsc.pack(fill="x")
            txt.bind("<Configure>", lambda *_: dsc.configure(wraplength=max(150, txt.winfo_width())))
            tile = dict(frame=t, parts=(t, dot, txt, ttl, dsc), dot=dot, ttl=ttl, dsc=dsc, hover=False,
                        enabled=True)

            def click():
                if tile["enabled"]:
                    self.mode = key
                    self.refresh()

            def hover(on):
                tile["hover"] = on
                self._paint_tile(key)

            make_clickable(tile["parts"], click, lambda: hover(True), lambda: hover(False))
            return tile

        def _paint_tile(self, key):
            P = self.app.P
            tile = self.tiles[key]
            sel, on = self.mode == key, tile["enabled"]
            bg = P["accent_soft"] if sel else P["surface"]
            border = P["accent"] if sel else (P["border_strong"] if tile["hover"] and on else P["border"])
            for w in tile["parts"]:
                w.configure(bg=bg, cursor="hand2" if on else "arrow")
            tile["frame"].configure(highlightbackground=border, highlightcolor=border)
            tile["dot"].configure(text="●" if sel else "○", fg=P["accent"] if sel else P["faint"])
            tile["ttl"].configure(fg=P["text"] if on else P["faint"])
            tile["dsc"].configure(fg=P["muted"] if on else P["faint"])

        def _stepper(self, form, row, value, lo, hi, step, note=""):
            app, P, F = self.app, self.app.P, self.app.F
            box = tk.Frame(form, bg=P["bg"])
            box.grid(row=row, column=1, sticky="w", pady=5)
            var = tk.StringVar(value=str(value))
            stepper = dict(var=var, lo=lo, hi=hi)

            def bump(d):
                cur = int_of(var.get(), lo, lo, hi)
                var.set(str(min(hi, max(lo, cur + d))))

            Btn(box, app, "−", lambda: bump(-step), size="sm").pack(side="left")
            ent = tk.Entry(box, textvariable=var, width=5, justify="center", font=F["body_b"], relief="flat",
                           bg=P["surface"], fg=P["text"], insertbackground=P["text"], highlightthickness=1,
                           highlightbackground=P["border"], highlightcolor=P["accent"],
                           selectbackground=P["select"], selectforeground=P["text"])
            ent.pack(side="left", ipady=4, padx=4)
            Btn(box, app, "+", lambda: bump(step), size="sm").pack(side="left")
            note_lbl = tk.Label(box, text=note, font=F["tiny"], bg=P["bg"], fg=P["faint"])
            note_lbl.pack(side="left", padx=(10, 0))
            var.trace_add("write", lambda *a: self.update_summary())
            return stepper, note_lbl

        def _toggle(self, parent, text, attr):
            app, P, F = self.app, self.app.P, self.app.F
            row = tk.Frame(parent, bg=P["bg"])
            row.pack(side="left", padx=(0, 22))
            chk = Check(row, app, getattr(self, attr), P["bg"])
            chk.pack(side="left")
            lbl = tk.Label(row, text=text, font=F["small"], bg=P["bg"], fg=P["text"])
            lbl.pack(side="left", padx=(8, 0))

            def flip():
                setattr(self, attr, not getattr(self, attr))
                chk.set(getattr(self, attr))

            make_clickable((row, chk, chk.lbl, lbl), flip)
            return chk

        def _bank_card(self, bank):
            app, P, F = self.app, self.app.P, self.app.F
            on = str(bank.path) in self.sel
            c = tk.Frame(self.list.inner, bg=P["surface"], highlightthickness=1)
            c.pack(fill="x", pady=(0, 8))
            chk = Check(c, app, on, P["surface"])
            chk.pack(side="left", padx=(16, 14), pady=16, anchor="n")
            txt = tk.Frame(c, bg=P["surface"])
            txt.pack(side="left", fill="x", expand=True, pady=12)
            ttl = tk.Label(txt, text=bank.title, font=F["body_b"], bg=P["surface"],
                           fg=P["text"] if on else P["muted"], anchor="w")
            ttl.pack(fill="x")
            sub = f"{len(bank.questions)} preguntas  ·  {bank.path.name}"
            sublbl = tk.Label(txt, text=sub, font=F["small"], bg=P["surface"], fg=P["faint"], anchor="w")
            sublbl.pack(fill="x")
            parts = [c, chk, chk.lbl, txt, ttl, sublbl]
            rm = tk.Label(c, text="×", font=F["h3"], bg=P["surface"], fg=P["faint"], cursor="hand2", padx=12)
            rm.pack(side="right", fill="y")
            rm.bind("<Button-1>", lambda *_: self.remove_bank(bank))
            rm.bind("<Enter>", lambda *_: rm.configure(fg=P["ko"]))
            rm.bind("<Leave>", lambda *_: rm.configure(fg=P["faint"]))
            if bank.warnings:
                warn = tk.Label(c, text=f"{len(bank.warnings)} avisos", font=F["tiny_b"], bg=P["ko_soft"],
                                fg=P["ko"], padx=8, pady=2)
                warn.pack(side="right", padx=(0, 4))
                parts.append(warn)

            def paint(hover):
                c.configure(highlightbackground=P["border_strong"] if hover else P["border"])

            make_clickable(parts, lambda: self.toggle_bank(bank), lambda: paint(True), lambda: paint(False))
            paint(False)

        # --- lista de bancos ---
        def fill_list(self):
            for w in self.list.inner.winfo_children():
                w.destroy()
            P, F = self.app.P, self.app.F
            if not self.app.banks:
                empty = card(self.list.inner, self.app)
                empty.pack(fill="x")
                tk.Label(empty, text="No hay bancos cargados", font=F["body_b"], bg=P["surface"],
                         fg=P["text"]).pack(pady=(26, 4))
                folder = subject_dir(self.app.subject).relative_to(APP_DIR)
                tk.Label(empty, text=f"Usa «Añadir fichero…» o pon ficheros .txt en la carpeta {folder}/.",
                         font=F["small"], bg=P["surface"], fg=P["muted"]).pack(pady=(0, 26))
            for b in self.app.banks:
                self._bank_card(b)
            n_warn = sum(len(b.warnings) for b in self.app.banks)
            if n_warn:
                self.warn_btn.configure(text=f"Ver avisos de los ficheros ({n_warn})")
                self.warn_btn.pack(side="right")
            else:
                self.warn_btn.pack_forget()
            self.list.top()

        def selected_banks(self) -> list[Bank]:
            return [b for b in self.app.banks if str(b.path) in self.sel]

        def toggle_bank(self, bank):
            key = str(bank.path)
            self.sel.symmetric_difference_update({key})
            self.fill_list()
            self.on_selection()

        def remove_bank(self, bank):
            self.app.banks = [b for b in self.app.banks if b.path != bank.path]
            self.sel.discard(str(bank.path))
            self.fill_list()
            self.on_selection()

        def toggle_all(self):
            paths = {str(b.path) for b in self.app.banks}
            self.sel = set() if self.sel >= paths else paths
            self.fill_list()
            self.on_selection()

        def copy_into_subject(self, path: Path) -> Path | None:
            """Copia el fichero a la carpeta de la asignatura para que se cargue siempre con ella."""
            folder = subject_dir(self.app.subject)
            dest = folder / path.name
            if path.resolve() == dest.resolve():
                return dest
            if dest.exists() and not self.app.dialog(
                    "El fichero ya existe",
                    f"Ya hay un {path.name} en {self.app.subject}. ¿Quieres reemplazarlo por el nuevo?",
                    ok="Reemplazar", cancel="Cancelar"):
                return None
            try:
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            except OSError as e:
                self.app.dialog("No se pudo copiar el fichero", f"{path}\n\n{e}")
                return None
            return dest

        def add_files(self):
            folder = subject_dir(self.app.subject)
            paths = filedialog.askopenfilenames(
                parent=self, title=f"Añadir bancos de preguntas a {self.app.subject}",
                initialdir=str(folder if folder.is_dir() else Path.home()),
                filetypes=[("Ficheros de texto", "*.txt"), ("Todos", "*")])
            warns = []
            for p in paths or ():
                dest = self.copy_into_subject(Path(p))
                if dest is None:
                    continue
                bank = self.app.add_bank(dest)
                if bank:
                    self.sel.add(str(bank.path))
                    warns += bank.warnings
            self.fill_list()
            self.on_selection()
            if warns:
                self.show_warnings(warns)

        def reload(self):
            paths = [b.path for b in self.app.banks]
            for p in subject_files(self.app.subject):
                if p.resolve() not in paths:
                    paths.append(p.resolve())
                    self.sel.add(str(p.resolve()))
            self.app.banks = []
            for p in paths:
                self.app.add_bank(p)
            self.fill_list()
            self.refresh()

        def show_warnings(self, warns=None):
            app, P, F = self.app, self.app.P, self.app.F
            warns = warns if warns is not None else [w for b in app.banks for w in b.warnings]
            win = tk.Toplevel(self)
            win.title("Avisos al leer los ficheros")
            win.configure(bg=P["surface"])
            win.geometry("860x440")
            tk.Label(win, text="Avisos al leer los ficheros", font=F["h2"], bg=P["surface"],
                     fg=P["text"]).pack(anchor="w", padx=24, pady=(20, 4))
            tk.Label(win, text="Estas preguntas se han saltado o tienen algo raro. Corrige el .txt y pulsa «Recargar».",
                     font=F["small"], bg=P["surface"], fg=P["muted"]).pack(anchor="w", padx=24, pady=(0, 12))
            Btn(win, app, "Cerrar", win.destroy, kind="primary", bg=P["surface"]).pack(
                side="bottom", anchor="e", padx=24, pady=16)
            t = tk.Text(win, font=F["mono"], bg=P["code_bg"], fg=P["text"], relief="flat", bd=0,
                        highlightthickness=0, padx=14, pady=10, wrap="word")
            t.pack(fill="both", expand=True, padx=24)
            t.insert("end", "\n".join(warns) or "Sin avisos.")
            t.configure(state="disabled")

        # --- configuración ---
        def pool(self, ignore_mode=False) -> list[Question]:
            seen, qs = set(), []
            topic = self.topic.get()
            for b in self.selected_banks():
                for q in b.questions:
                    if q.key in seen:
                        continue
                    if topic and topic != "Todos los temas" and q.topic != topic:
                        continue
                    if not ignore_mode and self.mode == "repaso" and q.key not in self.app.fails:
                        continue
                    seen.add(q.key)
                    qs.append(q)
            return qs

        def on_selection(self, initial=False):
            banks = self.selected_banks()
            first = str(banks[0].path) if banks else None
            if first and first != self._defaults_from and not (initial and self.app.config_state):
                s = banks[0].settings
                if "tiempo" in s:
                    self.min_step["var"].set(str(s["tiempo"]))
                if "penalizacion" in s:
                    p = s["penalizacion"]
                    self.pen_step["var"].set(str(int(p)) if p == int(p) else str(p))
                if "preguntas" in s:
                    self.n_step["var"].set(str(s["preguntas"]))
            self._defaults_from = first
            self.refresh()

        def refresh(self):
            P = self.app.P
            topics = sorted({q.topic for b in self.selected_banks() for q in b.questions if q.topic})
            values = ["Todos los temas"] + topics
            self.topic_cb.configure(values=values)
            if self.topic.get() not in values:
                self.topic.set(values[0])
            base = self.pool(ignore_mode=True)
            n_fails = sum(1 for q in base if q.key in self.app.fails)
            rep = self.tiles["repaso"]
            rep["enabled"] = n_fails > 0
            rep["dsc"].configure(text=(f"Solo las {n_fails} preguntas que has fallado en esta selección."
                                       if n_fails else "Aún no has fallado ninguna pregunta de esta selección."))
            if self.mode == "repaso" and not n_fails:
                self.mode = "practica"
            for key in self.tiles:
                self._paint_tile(key)
            total = len(self.app.fails)
            subject = self.app.subject
            self.fails_lbl.configure(text=f"{total} preguntas falladas guardadas en {subject}" if total
                                     else f"No tienes preguntas falladas guardadas en {subject}")
            if total:
                self.clear_btn.pack(side="left", padx=(10, 0))
            else:
                self.clear_btn.pack_forget()
            n_q = sum(len(b.questions) for b in self.selected_banks())
            self.count_lbl.configure(text=f"{len(self.sel)} de {len(self.app.banks)} seleccionados · {n_q} preguntas")
            self.update_summary()

        def update_summary(self):
            pool = self.pool()
            n = min(int_of(self.n_step["var"].get(), 15, 1), len(pool))
            minutes = int_of(self.min_step["var"].get(), 0, 0)
            pen = self.pen_step["var"].get().strip()
            self.n_note.configure(text=f"de {len(pool)} disponibles")
            parts = [f"{n} preguntas" if n != 1 else "1 pregunta",
                     f"{minutes} min" if minutes else "sin límite de tiempo",
                     f"cada {pen} fallos restan 1" if pen not in ("", "0") else "sin penalización"]
            self.summary.configure(text="  ·  ".join(parts) if pool else "No hay preguntas con esta selección.")
            self.start_btn.set_enabled(bool(pool))

        def clear_fails(self):
            if self.app.dialog("Borrar fallos guardados",
                               f"Se olvidarán las preguntas que tenías marcadas como falladas en "
                               f"{self.app.subject} (el historial completo se conserva). "
                               "No se puede deshacer.", ok="Borrar", cancel="Cancelar", danger=True):
                self.app.fails.clear()
                self.refresh()

        def save_state(self):
            self.app.config_state = dict(
                mode=self.mode, topic=self.topic.get(), n=int_of(self.n_step["var"].get(), 15, 1),
                minutes=int_of(self.min_step["var"].get(), 0, 0), pen=self.pen_step["var"].get(),
                shuffle_q=self.shuffle_q, shuffle_o=self.shuffle_o, selected=sorted(self.sel),
                defaults_from=self._defaults_from)

        def start(self):
            pool = self.pool()
            if not pool:
                return
            self.save_state()
            st = self.app.config_state
            try:
                pen = max(0.0, float(str(st["pen"]).replace(",", ".")))
            except ValueError:
                pen = 0.0
            n = st["n"]
            banks = self.selected_banks()
            reparto = banks[0].settings.get("reparto") if len(banks) == 1 else None
            qs = sample_by_topic(pool, n, reparto) if self.shuffle_q else pool[:n]
            banks = self.selected_banks()
            title = banks[0].title if len(banks) == 1 else "Varios bancos"
            if self.topic.get() != "Todos los temas":
                title += f" · {self.topic.get()}"
            if self.mode == "repaso":
                title = "Repaso de fallos · " + title
            session = Session(mode=self.mode, items=make_items(qs, self.shuffle_o),
                              time_limit=st["minutes"] * 60, penalty=pen, title=title,
                              shuffle_questions=self.shuffle_q, shuffle_options=self.shuffle_o)
            self.app.switch(QuizScreen, session)

    # ─────────────────────────── Pantalla de test ───────────────────────────
    class OptionCard(tk.Frame):
        STATUS = {"correct": ("✓  Correcta", "ok"), "wrong": ("✗  Tu respuesta", "ko"),
                  "missed": ("✓  Era la correcta", "ok")}

        def __init__(self, parent, app, letter, text, on_click):
            P, F = app.P, app.F
            super().__init__(parent, highlightthickness=2)
            self.app, self.state, self.locked = app, "idle", False
            self.badge = tk.Label(self, text=letter, font=F["letter"], width=2, pady=2)
            self.badge.pack(side="left", anchor="n", padx=(14, 14), pady=12)
            self.status = tk.Label(self, text="", font=F["tiny_b"])
            self.status.pack(side="right", anchor="n", padx=(8, 16), pady=16)
            mono = "\n" in text
            self.label = tk.Label(self, text=text, font=F["mono"] if mono else F["option"],
                                  justify="left", anchor="w")
            self.label.pack(side="left", fill="x", expand=True, pady=13)
            self.bind("<Configure>", lambda *_: self._wrap())
            make_clickable((self, self.badge, self.label, self.status), on_click,
                           lambda: self._hover(True), lambda: self._hover(False))
            self.paint("idle")

        def _wrap(self):
            reserve = 76 + (self.status.winfo_reqwidth() + 24 if self.status.cget("text") else 12)
            self.label.configure(wraplength=max(200, self.winfo_width() - reserve))

        def _hover(self, on):
            if not self.locked and self.state in ("idle", "hover"):
                self.paint("hover" if on else "idle")

        def paint(self, state):
            P = self.app.P
            self.state = state
            look = {
                "idle": (P["surface"], P["border"], P["surface2"], P["muted"], P["text"]),
                "hover": (P["surface"], P["border_strong"], P["surface2"], P["text"], P["text"]),
                "selected": (P["accent_soft"], P["accent"], P["accent"], P["accent_text"], P["text"]),
                "correct": (P["ok_soft"], P["ok"], P["ok"], P["on_ok"], P["text"]),
                "wrong": (P["ko_soft"], P["ko"], P["ko"], P["on_ko"], P["text"]),
                "missed": (P["surface"], P["ok"], P["ok"], P["on_ok"], P["text"]),
                "dim": (P["surface"], P["border"], P["surface2"], P["faint"], P["muted"]),
            }[state]
            bg, border, badge_bg, badge_fg, fg = look
            self.configure(bg=bg, highlightbackground=border, highlightcolor=border)
            self.badge.configure(bg=badge_bg, fg=badge_fg)
            self.label.configure(bg=bg, fg=fg)
            text, color = self.STATUS.get(state, ("", "muted"))
            self.status.configure(text=text, bg=bg, fg=P[color])
            cursor = "arrow" if self.locked else "hand2"
            for w in (self, self.badge, self.label, self.status):
                w.configure(cursor=cursor)
            self._wrap()

    class QuizScreen(tk.Frame):
        def __init__(self, app: App, session: Session, review_items: list[Item] | None = None):
            super().__init__(app, bg=app.P["bg"])
            self.app, self.session = app, session
            self.review = review_items is not None
            self.items = review_items if self.review else session.items
            self.exam = session.is_exam and not self.review
            self.practice = not self.exam and not self.review
            self.index = 0
            self._timer_job = None
            self.cards: list[OptionCard] = []
            self._build()
            self.show(0)
            if not self.review:
                self._tick()

        def destroy(self):
            if self._timer_job:
                self.after_cancel(self._timer_job)
                self._timer_job = None
            super().destroy()

        def _build(self):
            app, P, F = self.app, self.app.P, self.app.F
            # barra superior
            top = tk.Frame(self, bg=P["surface"])
            top.pack(fill="x")
            row = tk.Frame(top, bg=P["surface"])
            row.pack(fill="x", padx=28, pady=13)
            mode = "Revisión" if self.review else ("Examen" if self.exam else
                                                   "Repaso" if self.session.mode == "repaso" else "Práctica")
            tk.Label(row, text=mode.upper(), font=F["tiny_b"], bg=P["accent_soft"], fg=P["accent"],
                     padx=8, pady=3).pack(side="left")
            title = self.session.title.replace("Repaso de fallos · ", "")
            tk.Label(row, text=title, font=F["body_b"], bg=P["surface"], fg=P["text"]).pack(side="left", padx=12)
            self.timer_lbl = tk.Label(row, font=F["h3"], bg=P["surface2"], fg=P["text"], padx=12, pady=3)
            if not self.review:
                self.timer_lbl.pack(side="right")
            self.score_lbl = tk.Label(row, font=F["small_b"], bg=P["surface"], fg=P["muted"])
            self.score_lbl.pack(side="right", padx=16)
            self.progress = Bar(self, app, [], height=3, bg=P["surface"], track=P["border"])
            self.progress.pack(fill="x")

            # cuadrícula
            self.cells = []
            if len(self.items) <= 120:
                band = tk.Frame(self, bg=P["bg"])
                band.pack(fill="x", pady=(14, 0))
                grid = tk.Frame(band, bg=P["bg"])
                grid.pack()
                cols = 20 if len(self.items) > 40 else min(len(self.items), 20)
                for i in range(len(self.items)):
                    cell = tk.Label(grid, text=str(i + 1), font=F["tiny_b"], width=3, pady=3, bd=0,
                                    highlightthickness=2)
                    cell.grid(row=i // cols, column=i % cols, padx=2, pady=2)
                    cell.bind("<Button-1>", lambda *_, i=i: self.show(i))
                    cell.configure(cursor="hand2")
                    self.cells.append(cell)

            # barra inferior
            bottom = tk.Frame(self, bg=P["surface"])
            bottom.pack(side="bottom", fill="x")
            tk.Frame(self, bg=P["border"], height=1).pack(side="bottom", fill="x")
            bar = tk.Frame(bottom, bg=P["surface"])
            bar.pack(fill="x", padx=28, pady=12)
            S = P["surface"]
            self.prev_btn = self.next_btn = self.primary_btn = None
            if self.practice:
                Btn(bar, app, "Terminar", self.finish_early, kind="ghost", bg=S).pack(side="left")
                self.primary_btn = Btn(bar, app, "", self.primary, kind="primary", size="lg", bg=S)
                self.primary_btn.pack(side="right")
                # para volver a ver las preguntas ya corregidas
                self.prev_btn = Btn(bar, app, "←  Anterior", self.prev, size="lg", bg=S)
                self.prev_btn.pack(side="right", padx=10)
                keys = "a–h o 1–9 eligen  ·  Enter corrige y avanza  ·  ← → navegan"
            elif self.exam:
                Btn(bar, app, "Abandonar", self.abandon, kind="ghost", bg=S).pack(side="left")
                Btn(bar, app, "Borrar respuesta", self.clear_answer, kind="ghost", bg=S).pack(side="left", padx=4)
                Btn(bar, app, "Entregar", self.submit, kind="primary", size="lg", bg=S).pack(side="right")
                self.next_btn = Btn(bar, app, "Siguiente  →", self.next, size="lg", bg=S)
                self.next_btn.pack(side="right", padx=10)
                self.prev_btn = Btn(bar, app, "←  Anterior", self.prev, size="lg", bg=S)
                self.prev_btn.pack(side="right")
                keys = "a–h o 1–9 eligen  ·  ← → navegan  ·  Supr borra"
            else:
                Btn(bar, app, "←  Volver a resultados", self.back_to_results, kind="ghost", bg=S).pack(side="left")
                self.next_btn = Btn(bar, app, "Siguiente  →", self.next, size="lg", bg=S)
                self.next_btn.pack(side="right")
                self.prev_btn = Btn(bar, app, "←  Anterior", self.prev, size="lg", bg=S)
                self.prev_btn.pack(side="right", padx=10)
                keys = "← → navegan"
            self.copy_btn = Btn(bar, app, "Copiar pregunta", self.copy_question, kind="ghost", bg=S)
            self.copy_btn.pack(side="left", padx=4)
            keys += "  ·  Ctrl+C copia"
            tk.Label(bar, text=keys, font=F["tiny"], bg=S, fg=P["faint"]).pack(side="left", expand=True)

            # contenido
            self.scroller = Scroller(self, app, bg=P["bg"], maxwidth=860, padx=32, pady=22)
            self.scroller.pack(fill="both", expand=True)
            col = self.scroller.inner
            meta = tk.Frame(col, bg=P["bg"])
            meta.pack(fill="x")
            self.progress_lbl = tk.Label(meta, font=F["tiny_b"], bg=P["bg"], fg=P["faint"])
            self.progress_lbl.pack(side="left")
            self.meta_lbl = tk.Label(meta, font=F["tiny"], bg=P["bg"], fg=P["faint"])
            self.meta_lbl.pack(side="right")
            self.q_text = RichText(col, app, F["question"], bg=P["bg"])
            self.q_text.pack(fill="x", pady=(8, 4))
            self.hint = tk.Label(col, font=F["tiny_b"], bg=P["accent_soft"], fg=P["accent"], padx=8, pady=3)
            self.opts = tk.Frame(col, bg=P["bg"])
            self.opts.pack(fill="x", pady=(14, 0))

            self.expl = tk.Frame(col, bg=P["surface"], highlightthickness=1, highlightbackground=P["border"])
            self.stripe = tk.Frame(self.expl, width=5, bg=P["ok"])
            self.stripe.pack(side="left", fill="y")
            ebody = tk.Frame(self.expl, bg=P["surface"])
            ebody.pack(side="left", fill="both", expand=True, padx=20, pady=16)
            self.verdict = tk.Label(ebody, font=F["h3"], bg=P["surface"], anchor="w", justify="left")
            self.verdict.pack(fill="x", pady=(0, 6))
            self.e_text = RichText(ebody, app, F["body"], bg=P["surface"], sources=True)
            self.e_text.pack(fill="x")

        def _locked(self, item: Item) -> bool:
            return self.review or (self.practice and item.checked)

        # --- mostrar una pregunta ---
        def show(self, i: int) -> None:
            self.index = i
            item = self.items[i]
            q = item.q
            self.progress_lbl.configure(text=f"PREGUNTA {i + 1} DE {len(self.items)}")
            meta = [x for x in (q.topic, f"{Path(q.source).name}:{q.line}" if q.source else "") if x]
            self.meta_lbl.configure(text="   ·   ".join(meta))
            self.q_text.set(q.text)
            if q.multiple:
                self.hint.configure(text=f"RESPUESTA MÚLTIPLE · MARCA {len(q.correct)}")
                self.hint.pack(anchor="w", pady=(10, 0), before=self.opts)
            else:
                self.hint.pack_forget()
            for w in self.opts.winfo_children():
                w.destroy()
            self.cards = []
            for pos, orig in enumerate(item.order):
                c = OptionCard(self.opts, self.app, chr(97 + pos), clean_option(q.options[orig]),
                               lambda pos=pos: self.select_key(pos))
                c.pack(fill="x", pady=(0, 8))
                self.cards.append(c)
            if self._locked(item):
                self._show_correction(item)
            else:
                self.expl.pack_forget()
                self._paint_selection(item)
            self._update_buttons()
            self._update_grid()
            self._update_score()
            self.scroller.top()

        def _paint_selection(self, item: Item) -> None:
            for pos, c in enumerate(self.cards):
                c.locked = False
                c.paint("selected" if pos in item.selected else "idle")

        def _show_correction(self, item: Item) -> None:
            P = self.app.P
            q = item.q
            for pos, c in enumerate(self.cards):
                good = item.order[pos] in q.correct
                chosen = pos in item.selected
                c.locked = True
                c.paint("correct" if good and chosen else "missed" if good else "wrong" if chosen else "dim")
            status = item.status()
            letters = item.correct_letters()
            plural = len(q.correct) > 1
            if status == "ok":
                text, color = "¡Correcto!", P["ok"]
            elif status == "ko":
                text = f"Incorrecto. {'Las correctas son' if plural else 'La correcta es la'} {letters}."
                color = P["ko"]
            else:
                text = f"Sin contestar. {'Las correctas son' if plural else 'La correcta es la'} {letters}."
                color = P["muted"]
            self.verdict.configure(text=text, fg=color)
            self.stripe.configure(bg=color)
            self.e_text.set(remap_letters(q.explanation, item) or "Esta pregunta no tiene explicación.")
            self.expl.pack(fill="x", pady=(16, 0))

        def select_key(self, pos: int) -> None:
            item = self.items[self.index]
            if self._locked(item) or not 0 <= pos < len(item.order):
                return
            if item.q.multiple:
                item.selected ^= {pos}
            else:
                item.selected = set() if item.selected == {pos} and self.exam else {pos}
            self._paint_selection(item)
            self._update_grid()
            self._update_score()
            self._update_buttons()

        def copy_question(self) -> None:
            """Copia la pregunta al portapapeles para poder pegarla en otro sitio.
            La respuesta y la explicación solo se copian si la pregunta ya está corregida."""
            item = self.items[self.index]
            q = item.q
            lines = [q.text, ""]
            for pos, orig in enumerate(item.order):
                lines.append(f"{chr(97 + pos)}) {q.options[orig]}")
            if self._locked(item):
                mine = ", ".join(chr(97 + pos) for pos in sorted(item.selected)) or "en blanco"
                lines += ["", f"Mi respuesta: {mine}", f"Respuesta correcta: {item.correct_letters()}"]
                if q.explanation:
                    lines += ["", "Explicación: " + remap_letters(q.explanation, item)]
            if q.source:
                where = f"{Path(q.source).name}:{q.line}"
                lines += ["", f"({q.topic} · {where})" if q.topic else f"({where})"]
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self.copy_btn.configure(text="Copiada ✓")
            self.after(1500, self._reset_copy_btn)

        def _reset_copy_btn(self) -> None:
            # puede que ya se haya cambiado de pantalla
            if self.copy_btn.winfo_exists():
                self.copy_btn.configure(text="Copiar pregunta")

        def clear_answer(self) -> None:
            item = self.items[self.index]
            if not self._locked(item):
                item.selected = set()
                self._paint_selection(item)
                self._update_grid()
                self._update_score()
                self._update_buttons()

        # --- estado visual ---
        def _update_grid(self) -> None:
            P = self.app.P
            for i, (cell, item) in enumerate(zip(self.cells, self.items)):
                if self._locked(item):
                    bg, fg = {"ok": (P["ok"], P["on_ok"]), "ko": (P["ko"], P["on_ko"]),
                              "blank": (P["blank_soft"], P["muted"])}[item.status()]
                elif self.exam and item.selected:
                    bg, fg = P["accent_soft"], P["accent"]
                else:
                    bg, fg = P["surface"], P["faint"]
                ring = P["text"] if i == self.index else (P["border"] if bg == P["surface"] else bg)
                cell.configure(bg=bg, fg=fg, highlightbackground=ring, highlightcolor=ring)
            if self.review:
                done = len(self.items)
            elif self.exam:
                done = sum(1 for it in self.items if it.selected)
            else:
                done = sum(1 for it in self.items if it.checked)
            self.progress.set([(done / max(1, len(self.items)), P["accent"])])

        def _update_score(self) -> None:
            P = self.app.P
            if self.practice:
                st = [i.status() for i in self.items if i.checked]
                self.score_lbl.configure(text=f"✓ {st.count('ok')}     ✗ {st.count('ko')}     — {st.count('blank')}")
            elif self.exam:
                done = sum(1 for i in self.items if i.selected)
                self.score_lbl.configure(text=f"{done} de {len(self.items)} contestadas")
            else:
                st = [i.status() for i in self.items]
                self.score_lbl.configure(text=f"✓ {st.count('ok')}     ✗ {st.count('ko')}     — {st.count('blank')}")

        def _update_buttons(self) -> None:
            last = self.index == len(self.items) - 1
            if self.practice:
                item = self.items[self.index]
                if not item.checked:
                    self.primary_btn.configure(text="Corregir" if item.selected else "Ver respuesta")
                else:
                    self.primary_btn.configure(text="Ver resultados  →" if last else "Siguiente  →")
                self.prev_btn.set_enabled(self.index > 0)
            else:
                self.prev_btn.set_enabled(self.index > 0)
                self.next_btn.set_enabled(not last)

        def _tick(self) -> None:
            P = self.app.P
            s = self.session
            elapsed = time.monotonic() - s.started
            if s.time_limit:
                remaining = s.time_limit - elapsed
                if remaining <= 0:
                    self._timer_job = None
                    self.timer_lbl.configure(text="00:00", bg=P["ko_soft"], fg=P["ko"])
                    self.app.dialog("Se acabó el tiempo",
                                    "El examen se entrega con lo que tengas contestado." if self.exam
                                    else "Vamos con los resultados de lo que has corregido.", ok="Ver resultados")
                    if self.exam:
                        self.submit(confirm=False)
                    else:
                        self.finish()
                    return
                low = remaining < 60
                self.timer_lbl.configure(text=fmt_time(remaining), bg=P["ko_soft"] if low else P["surface2"],
                                         fg=P["ko"] if low else P["text"])
            else:
                self.timer_lbl.configure(text=fmt_time(elapsed), fg=P["muted"])
            self._timer_job = self.after(500, self._tick)

        # --- acciones ---
        def primary(self) -> None:
            if self.practice:
                item = self.items[self.index]
                if not item.checked:
                    self.check()
                elif self.index < len(self.items) - 1:
                    self.show(self.index + 1)
                else:
                    self.finish()
            else:
                self.next()

        def check(self) -> None:
            item = self.items[self.index]
            item.checked = True
            self.app.fails.record(item.q, item.status() == "ok")
            self._show_correction(item)
            self._update_buttons()
            self._update_grid()
            self._update_score()
            self.after(80, lambda: self.scroller.see(self.expl))

        def next(self) -> None:
            if self.index < len(self.items) - 1:
                self.show(self.index + 1)

        def prev(self) -> None:
            if self.index > 0:
                self.show(self.index - 1)

        def finish(self) -> None:
            self.session.finished = time.monotonic()
            if not self.session.scored():
                self.app.show_start()
            else:
                self.app.switch(ResultsScreen, self.session)

        def finish_early(self) -> None:
            if not any(i.checked for i in self.items):
                self.app.show_start()
            elif self.app.dialog("¿Terminar la práctica?",
                                 "Verás los resultados de las preguntas que has corregido hasta ahora.",
                                 ok="Ver resultados", cancel="Seguir practicando"):
                self.finish()

        def submit(self, confirm: bool = True) -> None:
            blanks = sum(1 for i in self.items if not i.selected)
            if confirm:
                msg = (f"Te quedan {blanks} preguntas sin contestar. Las que estén en blanco no restan."
                       if blanks else "Has contestado todas las preguntas.")
                if not self.app.dialog("¿Entregar el examen?", msg, ok="Entregar", cancel="Seguir"):
                    return
            self.session.finished = time.monotonic()
            for item in self.items:
                self.app.fails.record(item.q, item.status() == "ok", save=False)
            self.app.fails.save()
            self.app.switch(ResultsScreen, self.session)

        def abandon(self) -> None:
            if self.app.dialog("¿Abandonar el examen?", "No se guardará nada de este intento.",
                               ok="Abandonar", cancel="Seguir", danger=True):
                self.app.show_start()

        def back_to_results(self) -> None:
            self.app.switch(ResultsScreen, self.session)

        def on_key(self, e) -> None:
            k, ch = e.keysym, e.char
            if k.lower() == "c" and e.state & 0x4:  # Ctrl+C: copiar, no elegir la opción c
                self.copy_question()
            elif k in ("Return", "KP_Enter"):
                self.primary()
            elif k == "Left":
                self.prev()
            elif k == "Right":
                self.next()
            elif k in ("BackSpace", "Delete") and self.exam:
                self.clear_answer()
            elif len(ch) == 1 and "a" <= ch.lower() <= "z":
                self.select_key(ord(ch.lower()) - 97)
            elif len(ch) == 1 and "1" <= ch <= "9":
                self.select_key(int(ch) - 1)

    # ─────────────────────────── Resultados ───────────────────────────
    class ResultsScreen(tk.Frame):
        def __init__(self, app: App, session: Session):
            P, F = app.P, app.F
            super().__init__(app, bg=P["bg"])
            self.app, self.session = app, session
            sc = session.score()
            items = session.scored()
            wrong = [i for i in items if i.status() != "ok"]
            passed = sc["grade"] >= 5

            scroller = Scroller(self, app, bg=P["bg"], maxwidth=780, padx=32, pady=36)
            scroller.pack(fill="both", expand=True)
            col = scroller.inner

            section_label(col, app, session.title).pack(fill="x")
            hero = tk.Frame(col, bg=P["bg"])
            hero.pack(fill="x", pady=(10, 4))
            tk.Label(hero, text=num(sc["grade"]), font=F["display"], bg=P["bg"], fg=P["text"]).pack(side="left")
            tk.Label(hero, text="/ 10", font=F["h2"], bg=P["bg"], fg=P["faint"]).pack(
                side="left", anchor="s", padx=(8, 0), pady=(0, 14))
            tk.Label(hero, text="APTO" if passed else "NO APTO", font=F["h3"],
                     bg=P["ok"] if passed else P["ko"], fg=P["on_ok"] if passed else P["on_ko"],
                     padx=14, pady=5).pack(side="right", anchor="s", pady=(0, 16))
            total = max(1, sc["total"])
            tk.Label(col, text=f"{sc['ok']} aciertos de {sc['total']} preguntas en {fmt_time(sc['elapsed'])}",
                     font=F["body"], bg=P["bg"], fg=P["muted"], anchor="w").pack(fill="x")
            Bar(col, app, [(sc["ok"] / total, P["ok"]), (sc["ko"] / total, P["ko"]),
                           (sc["blank"] / total, P["border_strong"])], height=10).pack(fill="x", pady=(16, 22))

            tiles = tk.Frame(col, bg=P["bg"])
            tiles.pack(fill="x")
            pen = f"−{num(sc['penalty'])}" if session.penalty else "—"
            data = [("Aciertos", str(sc["ok"]), P["ok"]), ("Fallos", str(sc["ko"]), P["ko"]),
                    ("En blanco", str(sc["blank"]), P["muted"]), ("Penalización", pen, P["text"]),
                    ("Puntos netos", num(sc["net"]), P["text"])]
            for i, (label, value, color) in enumerate(data):
                tiles.columnconfigure(i, weight=1, uniform="t")
                t = card(tiles, app)
                t.grid(row=0, column=i, sticky="nsew", padx=(0, 0 if i == len(data) - 1 else 8))
                tk.Label(t, text=value, font=F["h2"], bg=P["surface"], fg=color, anchor="w").pack(
                    fill="x", padx=16, pady=(14, 0))
                tk.Label(t, text=label, font=F["tiny"], bg=P["surface"], fg=P["muted"], anchor="w").pack(
                    fill="x", padx=16, pady=(0, 14))
            if session.penalty:
                tk.Label(col, text=f"Cada {session.penalty:g} fallos restan un acierto; las preguntas en blanco no restan.",
                         font=F["tiny"], bg=P["bg"], fg=P["faint"], anchor="w").pack(fill="x", pady=(8, 0))

            topics: dict[str, list[int]] = {}
            for it in items:
                if it.q.topic:
                    t = topics.setdefault(it.q.topic, [0, 0])
                    t[0] += it.status() == "ok"
                    t[1] += 1
            if len(topics) > 1:
                section_label(col, app, "Por temas").pack(fill="x", pady=(28, 10))
                box = card(col, app)
                box.pack(fill="x")
                box.columnconfigure(1, weight=1)
                for r, (name, (ok, tot)) in enumerate(sorted(topics.items())):
                    pady = (14 if r == 0 else 6, 14 if r == len(topics) - 1 else 6)
                    tk.Label(box, text=name, font=F["small"], bg=P["surface"], fg=P["text"], anchor="w").grid(
                        row=r, column=0, sticky="w", padx=(18, 16), pady=pady)
                    color = P["ok"] if ok / tot >= 0.5 else P["ko"]
                    Bar(box, app, [(ok / tot, color)], height=6, bg=P["surface"]).grid(
                        row=r, column=1, sticky="ew", pady=pady)
                    tk.Label(box, text=f"{ok}/{tot}", font=F["small_b"], bg=P["surface"], fg=color, width=6,
                             anchor="e").grid(row=r, column=2, padx=(10, 18), pady=pady)

            actions = tk.Frame(col, bg=P["bg"])
            actions.pack(fill="x", pady=(30, 0))
            if wrong:
                Btn(actions, app, f"Practicar los {len(wrong)} fallos",
                    lambda: app.switch(QuizScreen, session.again([i.q for i in wrong], mode="practica")),
                    kind="primary", size="lg").pack(side="left")
                Btn(actions, app, "Revisar fallos", lambda: app.switch(QuizScreen, session, review_items=wrong),
                    size="lg").pack(side="left", padx=10)
            else:
                Btn(actions, app, "Volver al inicio", app.show_start, kind="primary", size="lg").pack(side="left")
            Btn(actions, app, "Revisar todas", lambda: app.switch(QuizScreen, session, review_items=items),
                size="lg").pack(side="left", padx=(0 if wrong else 10, 0))
            more = tk.Frame(col, bg=P["bg"])
            more.pack(fill="x", pady=(12, 0))
            Btn(more, app, "Repetir este test", lambda: app.switch(QuizScreen, session.again()),
                kind="ghost").pack(side="left")
            if wrong:
                Btn(more, app, "Volver al inicio", app.show_start, kind="ghost").pack(side="left", padx=4)

    app = App()
    if mainloop:
        app.mainloop()
    return app


# ═══════════════════════════════ main ═══════════════════════════════════

def check_files(files: list[str]) -> int:
    bad = 0
    for f in files:
        try:
            bank = load_bank(Path(f))
        except OSError as e:
            print(f"{f}: no se pudo leer ({e})")
            bad += 1
            continue
        multi = sum(q.multiple for q in bank.questions)
        no_expl = sum(not q.explanation for q in bank.questions)
        print(f"{f}: «{bank.title}», {len(bank.questions)} preguntas "
              f"({multi} de respuesta múltiple, {no_expl} sin explicación), {len(bank.warnings)} avisos")
        for w in bank.warnings:
            print("   ⚠", w)
        bad += bool(bank.warnings)
    return 1 if bad else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Test tipo autoescuela con preguntas en .txt")
    ap.add_argument("files", nargs="*", help="ficheros .txt de preguntas")
    ap.add_argument("--check", action="store_true", help="solo valida los ficheros y muestra avisos")
    ap.add_argument("--zoom", type=float, default=1.0, help="factor de tamaño de letra (p. ej. 1.3)")
    args = ap.parse_args()
    if args.check:
        files = args.files or [str(p) for s in list_subjects() for p in subject_files(s)]
        sys.exit(check_files(files))
    run_gui(args.files, args.zoom)


if __name__ == "__main__":
    main()
