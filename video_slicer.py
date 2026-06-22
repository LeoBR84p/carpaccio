#!/usr/bin/env python3
"""Corta um trecho de um vídeo sem reconverter (mesmo formato e qualidade).

Ao rodar, abre o explorador de arquivos para escolher o vídeo, pergunta o
momento de início (hh:mm:ss) e o de fim (hh:mm:ss) e gera um novo arquivo
com apenas o período selecionado, usando `ffmpeg -c copy` (rápido, sem perda).

Uso:
    uv run video_slicer.py            # GUI: escolhe arquivo e tempos
    uv run video_slicer.py video.mp4  # já recebe o arquivo, pergunta os tempos
"""

import subprocess
import sys
from pathlib import Path

FFMPEG = Path(__file__).parent / "ffmpeg" / "bin" / "ffmpeg.exe"
FFPROBE = Path(__file__).parent / "ffmpeg" / "bin" / "ffprobe.exe"

VIDEO_EXTS = ("*.mp4 *.mkv *.mov *.avi *.webm *.flv *.ts *.m4v *.wmv *.mpg *.mpeg")


def parse_time(text: str) -> float:
    """Converte 'hh:mm:ss', 'mm:ss' ou 'ss' (segundos podem ter decimais)."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"tempo inválido: '{text}' (use hh:mm:ss)")
    nums = [float(p) for p in parts]      # ValueError se não for número
    while len(nums) < 3:                  # completa para [h, m, s]
        nums.insert(0, 0.0)
    h, m, s = nums
    if m >= 60 or s >= 60:
        raise ValueError(f"minutos/segundos devem ser < 60: '{text}'")
    return h * 3600 + m * 60 + s


def fmt_label(seconds: float) -> str:
    """Segundos -> 'HHhMMmSSs' para compor o nome do arquivo de saída."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}h{m:02d}m{s:02d}s"


def probe_duration(path: Path) -> float | None:
    """Duração total do vídeo em segundos (None se ffprobe falhar)."""
    try:
        out = subprocess.run(
            [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return None


def pick_file() -> str | None:
    """Abre o explorador de arquivos; devolve o caminho ou None se cancelado."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Escolha o vídeo para cortar",
        filetypes=[("Vídeos", VIDEO_EXTS), ("Todos os arquivos", "*.*")],
    )
    root.destroy()
    return path or None


def ask_time(label: str, default: str, gui: bool) -> str | None:
    """Pergunta um tempo via diálogo (GUI) ou pelo terminal."""
    if gui:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        answer = simpledialog.askstring(
            "Corte de vídeo", f"{label} (hh:mm:ss):", initialvalue=default
        )
        root.destroy()
        return answer
    try:
        answer = input(f"{label} (hh:mm:ss) [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return answer or default


def slice_video(src: Path, start: float, dur: float) -> Path:
    """Corta [start, start+dur] copiando os streams; retorna o caminho de saída.

    Usa busca pela entrada (-ss antes de -i): rápida mesmo em cortes distantes.
    Como -c copy não recodifica, o início encaixa no keyframe mais próximo
    anterior (comportamento inerente a um corte sem perdas).
    """
    out = src.with_name(
        f"{src.stem}_corte_{fmt_label(start)}-{fmt_label(start + dur)}{src.suffix}"
    )
    cmd = [
        str(FFMPEG), "-y", "-hide_banner",
        "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{dur:.3f}", "-c", "copy", str(out),
    ]
    subprocess.run(cmd, check=True)
    return out


def report(msg: str, gui: bool, error: bool = False) -> None:
    """Mostra mensagem final por messagebox (GUI) e sempre no terminal."""
    print(("ERRO: " if error else "") + msg, file=sys.stderr if error else sys.stdout)
    if gui:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            (messagebox.showerror if error else messagebox.showinfo)(
                "Corte de vídeo", msg
            )
            root.destroy()
        except ImportError:
            pass


def main() -> None:
    if not FFMPEG.exists():
        print(f"ffmpeg não encontrado em {FFMPEG}", file=sys.stderr)
        sys.exit(1)

    # arquivo: argumento ou explorador de arquivos
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
        gui = False
    else:
        chosen = pick_file()
        if not chosen:
            print("Nenhum arquivo selecionado.")
            sys.exit(0)
        src = Path(chosen)
        gui = True

    if not src.exists():
        report(f"Arquivo não encontrado: {src}", gui, error=True)
        sys.exit(1)

    total = probe_duration(src)
    if total is not None:
        print(f"[{src.name}] duração: {fmt_label(total)} ({total:.1f}s)")

    start_str = ask_time("Início", "00:00:00", gui)
    if start_str is None:
        print("Cancelado.")
        sys.exit(0)
    default_end = fmt_label(total).replace("h", ":").replace("m", ":").rstrip("s") \
        if total else "00:00:10"
    end_str = ask_time("Fim", default_end, gui)
    if end_str is None:
        print("Cancelado.")
        sys.exit(0)

    try:
        start = parse_time(start_str)
        end = parse_time(end_str)
    except ValueError as e:
        report(str(e), gui, error=True)
        sys.exit(1)

    if end <= start:
        report(
            f"O fim ({end_str}) deve ser maior que o início ({start_str}).",
            gui, error=True,
        )
        sys.exit(1)
    if total is not None and start >= total:
        report(
            f"O início ({start_str}) está além do fim do vídeo ({fmt_label(total)}).",
            gui, error=True,
        )
        sys.exit(1)
    if total is not None and end > total + 1:
        print(f"[aviso] fim além da duração; cortando até o final ({fmt_label(total)}).")
        end = total

    dur = end - start
    print(f"[cortando {start_str} -> {end_str}  ({dur:.1f}s)...]")
    try:
        out = slice_video(src, start, dur)
    except subprocess.CalledProcessError as e:
        report(f"ffmpeg falhou (código {e.returncode}).", gui, error=True)
        sys.exit(1)

    report(f"Pronto: {out.name}", gui)


if __name__ == "__main__":
    main()
