#!/usr/bin/env python3
"""
Junta dois arquivos MP4 sem re-encodar (mantém codec, resolução, bitrate, etc.).

Abre um seletor de arquivos para escolher os dois vídeos e onde salvar a saída.
"""

import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

FFMPEG = r".\ffmpeg\bin\ffmpeg.exe"

MP4_TYPES = [("Vídeos MP4", "*.mp4"), ("Todos os arquivos", "*.*")]


def pick_input(root: tk.Tk, title: str) -> Path | None:
    path = filedialog.askopenfilename(parent=root, title=title, filetypes=MP4_TYPES)
    return Path(path) if path else None


def pick_output(root: tk.Tk, default: Path) -> Path | None:
    path = filedialog.asksaveasfilename(
        parent=root,
        title="Salvar vídeo unido como...",
        initialfile=default.name,
        defaultextension=".mp4",
        filetypes=MP4_TYPES,
    )
    return Path(path) if path else None


def join_videos(inputs: list[Path], output: Path) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        list_file = Path(f.name)
        for p in inputs:
            f.write(f"file '{p.resolve()}'\n")

    try:
        cmd = [
            FFMPEG,
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(output),
        ]
        print(f"Executando: {' '.join(cmd)}\n")
        subprocess.run(cmd, check=True)
        print(f"\nArquivo gerado: {output}")
    finally:
        list_file.unlink(missing_ok=True)


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python join_videos.py <quantidade de vídeos>")
        print("Exemplo: python join_videos.py 3")
        sys.exit(1)

    try:
        count = int(sys.argv[1])
        if count < 2:
            raise ValueError
    except ValueError:
        print("Informe um número inteiro maior ou igual a 2.")
        sys.exit(1)

    root = tk.Tk()
    root.withdraw()

    inputs: list[Path] = []
    ordinals = ["1º", "2º", "3º", "4º", "5º", "6º", "7º", "8º", "9º", "10º"]
    for i in range(count):
        label = ordinals[i] if i < len(ordinals) else f"{i + 1}º"
        p = pick_input(root, f"Selecione o {label} vídeo ({i + 1}/{count})")
        if not p:
            sys.exit(0)
        inputs.append(p)

    default_output = inputs[0].with_name(inputs[0].stem + "_joined.mp4")
    output = pick_output(root, default_output)
    if not output:
        sys.exit(0)

    if output.exists():
        if not messagebox.askyesno("Substituir?", f"{output.name} já existe. Substituir?"):
            sys.exit(0)

    root.destroy()
    join_videos(inputs, output)
    print("Concluído.")


if __name__ == "__main__":
    main()
