#!/usr/bin/env python3
"""Amplifica (ou reduz) o volume do áudio de um vídeo, com redução de ruído opcional.

Ao rodar, abre o explorador de arquivos para escolher o vídeo e pergunta o
fator de volume (float): > 1 amplifica, < 1 reduz, 1 mantém. Em seguida
pergunta se deseja reduzir ruído e, em caso afirmativo, aplica um redutor.

O vídeo é copiado sem recodificar (-c:v copy) e só o áudio é processado com
o filtro `volume` (que não altera duração nem timestamps), preservando o
sincronismo. Tudo numa única passagem.

Uso:
    uv run audio_enhance.py            # GUI: escolhe arquivo e responde os prompts
    uv run audio_enhance.py video.mp4  # já recebe o arquivo
"""

import subprocess
import sys
from pathlib import Path

FFMPEG = Path(__file__).parent / "ffmpeg" / "bin" / "ffmpeg.exe"

VIDEO_EXTS = "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.ts *.m4v *.wmv *.mpg *.mpeg"

# codec de áudio compatível com cada contêiner (o vídeo é sempre copiado)
_AUDIO_CODEC = {".webm": "libopus", ".ogg": "libvorbis", ".ogv": "libvorbis"}

# redutor de ruído FFT embutido no ffmpeg (sem necessidade de modelo externo)
_DENOISE_FILTER = "afftdn=nf=-25"


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
        title="Escolha o vídeo para ajustar o volume",
        filetypes=[("Vídeos", VIDEO_EXTS), ("Todos os arquivos", "*.*")],
    )
    root.destroy()
    return path or None


def ask_gain(gui: bool) -> float | None:
    """Pergunta o fator de volume (float > 0). None se cancelado/ inválido."""
    prompt = "Fator de volume (>1 amplifica, <1 reduz, 1 = igual):"
    if gui:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        raw = simpledialog.askstring("Volume", prompt, initialvalue="2.0")
        root.destroy()
    else:
        try:
            raw = input(f"{prompt} [2.0]: ").strip() or "2.0"
        except (EOFError, KeyboardInterrupt):
            return None
    if raw is None:
        return None
    try:
        gain = float(raw.replace(",", "."))
    except ValueError:
        print(f"Valor inválido: '{raw}'", file=sys.stderr)
        return None
    return gain if gain > 0 else None


def ask_yes(question: str, gui: bool) -> bool:
    """Pergunta sim/não. Em GUI usa caixa de diálogo; no terminal, texto."""
    if gui:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        answer = messagebox.askyesno("Redução de ruído", question)
        root.destroy()
        return bool(answer)
    try:
        resp = input(f"{question} (s/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return resp in ("s", "sim", "y", "yes")


def enhance(src: Path, gain: float, denoise: bool) -> Path:
    """Aplica volume (e ruído opcional) ao áudio, copiando o vídeo. Saída: novo arquivo."""
    # cadeia de áudio: reduz ruído ANTES de amplificar (não amplifica o ruído)
    chain = f"{_DENOISE_FILTER},volume={gain}" if denoise else f"volume={gain}"

    tag = f"_vol{gain:g}" + ("_dn" if denoise else "")
    out = src.with_name(f"{src.stem}{tag}{src.suffix}")
    acodec = _AUDIO_CODEC.get(src.suffix.lower(), "aac")

    cmd = [
        str(FFMPEG), "-y", "-hide_banner",
        "-i", str(src),
        "-map", "0:v?", "-map", "0:a?",   # mantém vídeo e áudio (ignora dados)
        "-c:v", "copy",                    # vídeo intacto -> sincronismo preservado
        "-c:a", acodec, "-b:a", "192k",
        "-af", chain,
        str(out),
    ]
    subprocess.run(cmd, check=True)
    return out


def report(msg: str, gui: bool, error: bool = False) -> None:
    """Mostra a mensagem final por messagebox (GUI) e sempre no terminal."""
    print(("ERRO: " if error else "") + msg, file=sys.stderr if error else sys.stdout)
    if gui:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            (messagebox.showerror if error else messagebox.showinfo)(
                "Volume do áudio", msg
            )
            root.destroy()
        except ImportError:
            pass


def main() -> None:
    if not FFMPEG.exists():
        print(f"ffmpeg não encontrado em {FFMPEG}", file=sys.stderr)
        sys.exit(1)

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

    gain = ask_gain(gui)
    if gain is None:
        print("Cancelado.")
        sys.exit(0)

    denoise = ask_yes("Deseja reduzir o ruído do áudio?", gui)

    acao = "amplificando" if gain > 1 else "reduzindo" if gain < 1 else "mantendo"
    extra = " + redução de ruído" if denoise else ""
    print(f"[{acao} volume x{gain:g}{extra}...]")
    try:
        out = enhance(src, gain, denoise)
    except subprocess.CalledProcessError as e:
        report(f"ffmpeg falhou (código {e.returncode}).", gui, error=True)
        sys.exit(1)

    report(f"Pronto: {out.name}", gui)


if __name__ == "__main__":
    main()
