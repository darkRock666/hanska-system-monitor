#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HANSKA System Monitor: panel local de recursos para Ubuntu/Linux."""

from __future__ import annotations

import csv
import glob
import os
import platform
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path

import psutil


APP_NAME = "HANSKA System Monitor"
REFRESH_MS = 2000
MAX_PROCESSES = 40
BG = "#08090d"
PANEL = "#111217"
PANEL_ALT = "#1b1016"
FG = "#f6edf0"
MUTED = "#bdaeb3"
ACCENT = "#ff1744"
ACCENT_DARK = "#670016"
NEON_GLOW = "#ff4968"
GREEN = "#ff1744"
YELLOW = "#ff7890"
RED = "#ff003d"


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace").strip()
    except (OSError, ValueError):
        return ""


def cpu_model() -> str:
    info = read_text("/proc/cpuinfo")
    for line in info.splitlines():
        if line.lower().startswith(("model name", "hardware")) and ":" in line:
            return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine() or "CPU"


def cpu_temperature() -> float | None:
    try:
        for group in psutil.sensors_temperatures(fahrenheit=False).values():
            for item in group:
                if item.current is not None and item.current > 0:
                    return float(item.current)
    except (AttributeError, OSError, RuntimeError, NotImplementedError):
        pass

    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        value = read_text(path)
        try:
            temp = float(value)
            if temp > 1000:
                temp /= 1000
            if 0 < temp < 150:
                return temp
        except ValueError:
            continue
    return None


def _float_or_none(value: str) -> float | None:
    value = value.strip()
    if not value or value.upper() in {"N/A", "[N/A]", "NOT SUPPORTED"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int_or_none(value: str) -> int | None:
    number = _float_or_none(value)
    return int(number) if number is not None else None


def _lspci_gpu_names() -> list[str]:
    if not shutil.which("lspci"):
        return []
    try:
        result = subprocess.run(
            ["lspci", "-mm"], capture_output=True, text=True, timeout=1.5, check=False
        )
        return [
            re.sub(r'\s+', " ", line).strip().strip('"')
            for line in result.stdout.splitlines()
            if re.search(r"VGA compatible controller|3D controller|Display controller", line, re.I)
        ]
    except (OSError, subprocess.TimeoutExpired):
        return []


def collect_gpus() -> list[dict]:
    """Lee NVIDIA mediante nvidia-smi y AMD mediante las métricas disponibles en sysfs."""
    gpus: list[dict] = []
    nvidia_seen = False
    nvidia_error = "NVIDIA no detectada"

    if shutil.which("nvidia-smi"):
        query = [
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(query, capture_output=True, text=True, timeout=2.0, check=False)
            if result.returncode == 0:
                for row in csv.reader(result.stdout.splitlines()):
                    if not row:
                        continue
                    row += [""] * (5 - len(row))
                    gpus.append({
                        "name": row[0].strip(),
                        "vendor": "NVIDIA",
                        "load": _float_or_none(row[1]),
                        "memory_used": _float_or_none(row[2]),
                        "memory_total": _float_or_none(row[3]),
                        "temperature": _float_or_none(row[4]),
                        "memory_unit": "MiB",
                        "status": "Disponible",
                    })
                    nvidia_seen = True
            elif result.stderr:
                nvidia_error = result.stderr.strip().splitlines()[-1]
            else:
                nvidia_error = "Controlador NVIDIA no disponible"
        except (OSError, subprocess.TimeoutExpired):
            nvidia_error = "nvidia-smi no respondió"
    else:
        nvidia_error = "nvidia-smi no está instalado"

    amd_seen = False
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        device_dir = os.path.join(card, "device")
        if read_text(os.path.join(device_dir, "vendor")).lower() != "0x1002":
            continue

        amd_seen = True
        name = read_text(os.path.join(device_dir, "product_name"))
        device_id = read_text(os.path.join(device_dir, "device"))
        if not name:
            name = f"AMD/ATI (PCI {device_id})" if device_id else "AMD/ATI"
        load = _float_or_none(read_text(os.path.join(device_dir, "gpu_busy_percent")))
        used = _int_or_none(read_text(os.path.join(device_dir, "mem_info_vram_used")))
        total = _int_or_none(read_text(os.path.join(device_dir, "mem_info_vram_total")))
        unit = "bytes"
        if used is None or total is None:
            used = _int_or_none(read_text(os.path.join(device_dir, "mem_info_gtt_used")))
            total = _int_or_none(read_text(os.path.join(device_dir, "mem_info_gtt_total")))
            unit = "bytes compartidos"

        temperature = None
        for temp_path in glob.glob(os.path.join(device_dir, "hwmon", "hwmon*", "temp*_input")):
            try:
                value = float(read_text(temp_path))
                if value > 1000:
                    value /= 1000
                if 0 < value < 150:
                    temperature = value
                    break
            except ValueError:
                continue

        has_data = load is not None or used is not None or temperature is not None
        gpus.append({
            "name": name,
            "vendor": "AMD",
            "load": load,
            "memory_used": used,
            "memory_total": total,
            "temperature": temperature,
            "memory_unit": unit,
            "status": "Disponible" if has_data else "Detectada; métricas no expuestas por el controlador",
        })

    # Mantiene visible la GPU aunque el controlador no permita leer sus métricas.
    for name in _lspci_gpu_names():
        upper = name.upper()
        vendor = "NVIDIA" if "NVIDIA" in upper else "AMD" if "AMD" in upper or "ATI" in upper else ""
        if not vendor:
            continue
        if vendor == "NVIDIA" and not nvidia_seen:
            gpus.append({
                "name": name.split(":", 2)[-1].strip() or "NVIDIA",
                "vendor": vendor,
                "load": None,
                "memory_used": None,
                "memory_total": None,
                "temperature": None,
                "memory_unit": "MiB",
                "status": nvidia_error,
            })
            nvidia_seen = True
        elif vendor == "AMD" and not amd_seen:
            gpus.append({
                "name": name.split(":", 2)[-1].strip() or "AMD/ATI",
                "vendor": vendor,
                "load": None,
                "memory_used": None,
                "memory_total": None,
                "temperature": None,
                "memory_unit": "bytes",
                "status": "Detectada; métricas no expuestas por sysfs",
            })
            amd_seen = True

    return gpus


def collect_snapshot() -> dict:
    """Recolecta métricas sin esperar entre muestras; se llama fuera del hilo de Tk."""
    cpu = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    frequency = psutil.cpu_freq()
    processes = []

    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            meminfo = info.get("memory_info")
            processes.append({
                "pid": int(info["pid"]),
                "name": info.get("name") or "(sin nombre)",
                "cpu": float(proc.cpu_percent(interval=None)),
                "rss": int(meminfo.rss) if meminfo else 0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, TypeError, ValueError):
            continue

    return {
        "cpu": cpu,
        "per_core": per_core,
        "cpu_temp": cpu_temperature(),
        "memory": memory,
        "swap": swap,
        "frequency": frequency,
        "processes": processes,
        "gpus": collect_gpus(),
    }


def format_bytes(value: int | float) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def format_gpu_memory(gpu: dict) -> str:
    used, total = gpu.get("memory_used"), gpu.get("memory_total")
    if used is None or total is None:
        return "No disponible"
    if gpu.get("memory_unit") == "MiB":
        return f"{used:.0f} / {total:.0f} MiB"
    return f"{format_bytes(used)} / {format_bytes(total)}"


class SystemMonitor:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1080x720")
        self.root.minsize(820, 560)
        self.root.configure(background=BG)
        self.closed = False
        self.paused = False
        self.busy = False
        self.after_id = None
        self.sort_key = "cpu"
        self.sort_reverse = True
        self.latest_processes: list[dict] = []
        self.cpu_model = cpu_model()
        self.physical_cores = psutil.cpu_count(logical=False) or 0
        self.logical_cores = psutil.cpu_count(logical=True) or 0
        self.system_name = self._system_name()
        self._init_styles()
        self._build_ui()
        psutil.cpu_percent(interval=None)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.request_refresh()

    @staticmethod
    def _system_name() -> str:
        data = read_text("/etc/os-release")
        for line in data.splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
        return f"{platform.system()} {platform.release()}"

    def _init_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, font=("Sans", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground="#fff3f6", font=("Sans", 20, "bold"))
        style.configure("Value.TLabel", background=PANEL, foreground=FG, font=("Sans", 23, "bold"))
        style.configure("CardTitle.TLabel", background=PANEL, foreground=ACCENT, font=("Sans", 10, "bold"))
        style.configure("TButton", padding=(12, 7), background=PANEL_ALT, foreground=FG, borderwidth=1)
        style.map("TButton", background=[("active", ACCENT_DARK), ("pressed", ACCENT)], foreground=[("active", FG)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 9), background=PANEL_ALT, foreground=FG)
        style.map("TNotebook.Tab", background=[("selected", ACCENT_DARK)], foreground=[("selected", "#fff3f6")])
        style.configure("TLabelframe", background=BG, bordercolor=ACCENT_DARK)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=("Sans", 10, "bold"))
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=27)
        style.configure("Treeview.Heading", background=PANEL_ALT, foreground=ACCENT, padding=7)
        style.map("Treeview", background=[("selected", ACCENT_DARK)], foreground=[("selected", "#fff3f6")])
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL_ALT, background=ACCENT, thickness=12)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 14))
        titles = ttk.Frame(header)
        titles.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(titles, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            titles,
            text=f"{self.system_name}  •  {platform.node()}",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        self.neon_canvas = tk.Canvas(titles, height=10, background=BG, highlightthickness=0, bd=0)
        self.neon_canvas.pack(fill=tk.X, pady=(8, 0))
        self.neon_phase = 0
        self.root.after(50, self._animate_neon)
        self.pause_button = ttk.Button(header, text="Pausar", command=self.toggle_pause)
        self.pause_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(header, text="Actualizar", command=self.request_refresh).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="Leyendo métricas…")
        ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        self.overview_tab = ttk.Frame(self.tabs, padding=10)
        self.process_tab = ttk.Frame(self.tabs, padding=10)
        self.hardware_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.overview_tab, text="Resumen")
        self.tabs.add(self.process_tab, text="Procesos")
        self.tabs.add(self.hardware_tab, text="Componentes")

        self._build_overview()
        self._build_processes()
        self._build_hardware()

    def _animate_neon(self) -> None:
        if self.closed:
            return
        canvas = self.neon_canvas
        width = max(canvas.winfo_width(), 1)
        center_y = 5
        canvas.delete("all")
        canvas.create_line(2, center_y, width - 2, center_y, fill="#3a0712", width=2)
        if width > 2:
            position = self.neon_phase % (width + 140) - 70
            canvas.create_line(position - 50, center_y, position + 50, center_y,
                               fill="#650018", width=7, capstyle=tk.ROUND)
            canvas.create_line(position - 28, center_y, position + 28, center_y,
                               fill="#c90032", width=4, capstyle=tk.ROUND)
            canvas.create_line(position - 12, center_y, position + 12, center_y,
                               fill=NEON_GLOW, width=2, capstyle=tk.ROUND)
        self.neon_phase += 7
        self.root.after(50, self._animate_neon)

    def _card(self, parent: ttk.Frame, title: str, column: int) -> tuple[ttk.Frame, tk.StringVar, ttk.Progressbar]:
        card = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        card.grid(row=0, column=column, sticky="nsew", padx=5, pady=5)
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        value = tk.StringVar(value="—")
        ttk.Label(card, textvariable=value, style="Value.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 4))
        bar = ttk.Progressbar(card, mode="determinate", maximum=100)
        bar.grid(row=2, column=0, sticky="ew", pady=(2, 6))
        return card, value, bar

    def _build_overview(self) -> None:
        cards = ttk.Frame(self.overview_tab)
        cards.pack(fill=tk.X)
        for column in range(4):
            cards.columnconfigure(column, weight=1)
        _, self.cpu_value, self.cpu_bar = self._card(cards, "CPU", 0)
        _, self.ram_value, self.ram_bar = self._card(cards, "RAM", 1)
        _, self.swap_value, self.swap_bar = self._card(cards, "SWAP", 2)
        _, self.gpu_value, self.gpu_bar = self._card(cards, "GPU", 3)

        lower = ttk.Frame(self.overview_tab)
        lower.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        core_panel = ttk.LabelFrame(lower, text="Uso por núcleo", padding=12)
        core_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        self.core_rows = ttk.Frame(core_panel)
        self.core_rows.pack(fill=tk.BOTH, expand=True)

        gpu_panel = ttk.LabelFrame(lower, text="Gráficos y aceleradores", padding=12)
        gpu_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))
        self.gpu_text = tk.StringVar(value="Detectando GPU…")
        ttk.Label(gpu_panel, textvariable=self.gpu_text, justify=tk.LEFT, wraplength=400).pack(anchor="nw", fill=tk.X)
        self.summary_text = tk.StringVar(value="")
        ttk.Label(gpu_panel, textvariable=self.summary_text, style="Muted.TLabel", justify=tk.LEFT).pack(anchor="sw", fill=tk.X, expand=True, pady=(18, 0))

    def _build_processes(self) -> None:
        frame = ttk.Frame(self.process_tab)
        frame.pack(fill=tk.BOTH, expand=True)
        columns = ("pid", "name", "cpu", "ram")
        self.process_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        labels = {"pid": "PID", "name": "Proceso", "cpu": "CPU %", "ram": "RAM"}
        widths = {"pid": 90, "name": 430, "cpu": 110, "ram": 150}
        for key in columns:
            self.process_tree.heading(key, text=labels[key], command=lambda k=key: self.sort_processes(k))
            self.process_tree.column(key, width=widths[key], anchor="w" if key == "name" else "e")
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.process_tree.yview)
        self.process_tree.configure(yscrollcommand=scrollbar.set)
        self.process_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.process_count = tk.StringVar(value="Procesos: —")
        ttk.Label(self.process_tab, textvariable=self.process_count, style="Muted.TLabel").pack(anchor="w", pady=(7, 0))

    def _build_hardware(self) -> None:
        frame = ttk.LabelFrame(self.hardware_tab, text="Información de este equipo", padding=15)
        frame.pack(fill=tk.BOTH, expand=True, anchor="n")
        self.hardware_text = tk.StringVar(value="Leyendo componentes…")
        ttk.Label(frame, textvariable=self.hardware_text, justify=tk.LEFT, anchor="nw", wraplength=850).pack(fill=tk.BOTH, expand=True)

    def request_refresh(self) -> None:
        if self.closed or self.busy:
            return
        if self.after_id is not None:
            try:
                self.root.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        self.busy = True
        self.status_var.set("Actualizando…")
        threading.Thread(target=self._collect_worker, daemon=True).start()

    def _collect_worker(self) -> None:
        try:
            snapshot = collect_snapshot()
            error = None
        except Exception as exc:  # keep the UI alive if one metric source fails
            snapshot = None
            error = f"{type(exc).__name__}: {exc}"
        try:
            self.root.after(0, self._finish_refresh, snapshot, error)
        except tk.TclError:
            pass

    def _finish_refresh(self, snapshot: dict | None, error: str | None) -> None:
        self.busy = False
        if self.closed:
            return
        if error:
            self.status_var.set(f"No se pudieron leer algunas métricas: {error}")
        elif snapshot is not None:
            self.status_var.set("Actualizado ahora")
            self._render(snapshot)
        if not self.paused:
            self.after_id = self.root.after(REFRESH_MS, self.request_refresh)

    def _render(self, data: dict) -> None:
        cpu = data["cpu"]
        memory = data["memory"]
        swap = data["swap"]
        self.cpu_value.set(f"{cpu:.0f}%")
        self.cpu_bar["value"] = cpu
        self.ram_value.set(f"{memory.percent:.0f}%")
        self.ram_bar["value"] = memory.percent
        swap_pct = (swap.used / swap.total * 100) if swap.total else 0.0
        self.swap_value.set(f"{swap_pct:.0f}%" if swap.total else "Sin swap")
        self.swap_bar["value"] = swap_pct

        gpus = data["gpus"]
        gpu_loads = [gpu["load"] for gpu in gpus if gpu["load"] is not None]
        gpu_load = max(gpu_loads) if gpu_loads else 0
        self.gpu_value.set(f"{gpu_load:.0f}%" if gpu_loads else ("Detectada" if gpus else "No detectada"))
        self.gpu_bar["value"] = gpu_load
        self.gpu_text.set(self._gpu_summary(gpus))

        self._render_cores(data["per_core"])
        self.latest_processes = data["processes"]
        self._render_processes()
        temp = f"{data['cpu_temp']:.0f} °C" if data["cpu_temp"] is not None else "No disponible"
        freq = f"{data['frequency'].current:.0f} MHz" if data["frequency"] else "No disponible"
        self.summary_text.set(
            f"{self.cpu_model}\n"
            f"{self.physical_cores} núcleos físicos / {self.logical_cores} hilos\n"
            f"Frecuencia actual: {freq}  •  Temperatura CPU: {temp}\n"
            f"RAM: {format_bytes(memory.used)} usados de {format_bytes(memory.total)}"
        )
        self._render_hardware(data, temp, freq)

    def _render_cores(self, values: list[float]) -> None:
        for child in self.core_rows.winfo_children():
            child.destroy()
        for index, value in enumerate(values):
            row, column = divmod(index, 2)
            cell = ttk.Frame(self.core_rows)
            cell.grid(row=row, column=column, sticky="ew", padx=6, pady=5)
            self.core_rows.columnconfigure(column, weight=1)
            ttk.Label(cell, text=f"Núcleo {index + 1}", width=12).pack(side=tk.LEFT)
            bar = ttk.Progressbar(cell, maximum=100, value=value)
            bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 5))
            ttk.Label(cell, text=f"{value:.0f}%", width=5, style="Muted.TLabel").pack(side=tk.RIGHT)

    def _gpu_summary(self, gpus: list[dict]) -> str:
        if not gpus:
            return "No se detectó una GPU compatible."
        lines = []
        for gpu in gpus:
            load = f"{gpu['load']:.0f}%" if gpu["load"] is not None else "No disponible"
            temp = f"{gpu['temperature']:.0f} °C" if gpu["temperature"] is not None else "No disponible"
            lines.append(
                f"{gpu['name']} ({gpu['vendor']})\n"
                f"Uso: {load}  •  Memoria: {format_gpu_memory(gpu)}\n"
                f"Temperatura: {temp}  •  {gpu['status']}"
            )
        return "\n\n".join(lines)

    def _render_hardware(self, data: dict, temp: str, freq: str) -> None:
        memory = data["memory"]
        swap = data["swap"]
        lines = [
            f"Sistema operativo: {self.system_name}",
            f"Equipo: {platform.node()}",
            f"Arquitectura: {platform.machine()}",
            "",
            f"Procesador: {self.cpu_model}",
            f"Núcleos físicos: {self.physical_cores}",
            f"Hilos lógicos: {self.logical_cores}",
            f"Frecuencia actual: {freq}",
            f"Temperatura CPU: {temp}",
            f"RAM total: {format_bytes(memory.total)}",
            f"RAM disponible: {format_bytes(memory.available)}",
            f"RAM en uso: {format_bytes(memory.used)} ({memory.percent:.1f}%)",
            f"Swap total: {format_bytes(swap.total)}",
            f"Swap en uso: {format_bytes(swap.used)}",
            "",
            "GPU:",
        ]
        if data["gpus"]:
            for gpu in data["gpus"]:
                load = f"{gpu['load']:.0f}%" if gpu["load"] is not None else "No disponible"
                temp_value = f"{gpu['temperature']:.0f} °C" if gpu["temperature"] is not None else "No disponible"
                lines.append(f"• {gpu['name']} ({gpu['vendor']}) — uso {load}; memoria {format_gpu_memory(gpu)}; temperatura {temp_value}")
                lines.append(f"  Estado: {gpu['status']}")
        else:
            lines.append("• No se detectó GPU compatible.")
        lines.extend([
            "",
            "Las métricas dependen de lo que expongan los controladores del sistema.",
            "El uso de GPU por proceso no está disponible de forma uniforme en Linux.",
        ])
        self.hardware_text.set("\n".join(lines))

    def _render_processes(self) -> None:
        selected = self.process_tree.selection()
        selected_pid = selected[0] if selected else None
        sort_getters = {
            "cpu": lambda item: item["cpu"],
            "ram": lambda item: item["rss"],
            "name": lambda item: item["name"].lower(),
            "pid": lambda item: item["pid"],
        }
        rows = sorted(
            self.latest_processes,
            key=sort_getters.get(self.sort_key, sort_getters["cpu"]),
            reverse=self.sort_reverse,
        )[:MAX_PROCESSES]
        for item in self.process_tree.get_children():
            self.process_tree.delete(item)
        for proc in rows:
            pid = str(proc["pid"])
            self.process_tree.insert(
                "", "end", iid=pid,
                values=(pid, proc["name"], f"{proc['cpu']:.1f}%", format_bytes(proc["rss"])),
            )
        if selected_pid and self.process_tree.exists(selected_pid):
            self.process_tree.selection_set(selected_pid)
        self.process_count.set(f"Procesos visibles: {len(rows)} de {len(self.latest_processes)}")

    def sort_processes(self, column: str) -> None:
        key = "ram" if column == "ram" else "cpu" if column == "cpu" else "name" if column == "name" else "pid"
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = key in {"cpu", "ram"}
        self._render_processes()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_button.configure(text="Reanudar" if self.paused else "Pausar")
        if self.paused:
            if self.after_id is not None:
                try:
                    self.root.after_cancel(self.after_id)
                except tk.TclError:
                    pass
                self.after_id = None
            self.status_var.set("Actualización pausada")
        else:
            self.request_refresh()

    def close(self) -> None:
        self.closed = True
        if self.after_id is not None:
            try:
                self.root.after_cancel(self.after_id)
            except tk.TclError:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    SystemMonitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
