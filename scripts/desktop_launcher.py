"""PyInstaller entry point for the packaged desktop build. Kept tiny and import-light so PyInstaller's
static analysis has as little to miss as banana.desktop's own dynamic (string-based) uvicorn import."""

from banana.desktop import main

if __name__ == "__main__":
    main()
