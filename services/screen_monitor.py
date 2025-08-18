import win32gui
import win32process
import psutil

def get_foreground_app_info():
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    process = psutil.Process(pid)
    app_name = process.name()
    window_title = win32gui.GetWindowText(hwnd)
    return app_name, window_title
