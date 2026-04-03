import subprocess


def extract_text_with_tesseract(file_path: str) -> str:
    """
    Best-effort OCR.
    Returns empty text if tesseract is unavailable or fails.
    """
    try:
        result = subprocess.run(
            ["tesseract", file_path, "stdout"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except Exception:
        return ""

