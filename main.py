import ctypes
import shutil
import winreg

from rich.console import Console

from logo import print_logo

FFMPEG_DIR = r"C:\LEOBR\carpaccio\ffmpeg"

console = Console()


def add_to_user_path(directory: str) -> bool:
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    )
    current_path, _ = winreg.QueryValueEx(key, "Path")

    if directory in current_path.split(";"):
        winreg.CloseKey(key)
        return False

    winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, current_path + ";" + directory)
    winreg.CloseKey(key)
    ctypes.windll.user32.SendMessageW(0xFFFF, 0x001A, 0, "Environment")
    return True


def main():
    print_logo(console)

    if not shutil.which("ffmpeg"):
        if add_to_user_path(FFMPEG_DIR):
            console.print("[bright_green]ffmpeg adicionado ao PATH.[/]")
        else:
            console.print("[yellow]ffmpeg já estava no PATH do registro mas não foi encontrado no shell atual. Reinicie o terminal.[/]")


if __name__ == "__main__":
    main()
