from fastapi import APIRouter
from pydantic import BaseModel
import subprocess
import os
import logging
import webbrowser
import sys
import traceback

def log_crash(exc_type, exc_value, exc_traceback):
    with open("logs/crash.log", "a", encoding="utf-8") as f:
        f.write("=== CRASH DETECTED ===\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)

sys.excepthook = log_crash

router = APIRouter()

class TextInput(BaseModel):
    text: str

@router.post("/cli/run")
def run_cli_command(input_data: TextInput):
    try:
        output = subprocess.check_output(input_data.text, shell=True, stderr=subprocess.STDOUT)
        return {"output": output.decode("utf-8")}
    except subprocess.CalledProcessError as e:
        return {"error": e.output.decode("utf-8")}

def open_website(url, name="site"):
    try:
        webbrowser.open(url)
        logging.info(f"[CLI] Opened {name} in default browser.")
    except Exception as e:
        logging.error(f"[CLI] Failed to open {name}: {e}")

def process_command(command: str):
    command = command.lower()
    logging.debug(f"[CLI] Received command: {command}")

    if "clip that" in command:
        try:
            from screen_recorder import clip_recent_video
            clip_recent_video()
        except Exception as e:
            logging.error(f"[CLI] Clip error: {e}")

    # Desktop app commands
    elif "open spotify" in command:
        os.system("start spotify")
    elif "open discord" in command:
        os.system("start discord")
    elif "open steam" in command:
        os.system("start steam")
    elif "open obs" in command or "open recorder" in command:
        os.system("start obs64" if os.name == "nt" else "obs")
    elif "open epic" in command or "open epicgames" in command:
        os.system("start com.epicgames.launcher" if os.name == "nt" else "epic")

    # Web aliases (flexible phrases)
    elif any(phrase in command for phrase in ["google", "go to google", "launch google", "search google"]):
        open_website("https://www.google.com", "Google")
    elif any(phrase in command for phrase in ["youtube", "go to youtube", "launch youtube", "watch youtube"]):
        open_website("https://www.youtube.com", "YouTube")
    elif any(phrase in command for phrase in ["github", "go to github", "launch github"]):
        open_website("https://github.com", "GitHub")
    elif any(phrase in command for phrase in ["reddit", "open reddit", "go to reddit"]):
        open_website("https://www.reddit.com", "Reddit")
    elif any(phrase in command for phrase in ["chatgpt", "open chatgpt", "talk to chatgpt"]):
        open_website("https://chat.openai.com", "ChatGPT")
    elif any(phrase in command for phrase in ["gmail", "check email", "open gmail"]):
        open_website("https://mail.google.com", "Gmail")
    elif any(phrase in command for phrase in ["netflix", "watch netflix"]):
        open_website("https://www.netflix.com", "Netflix")
    elif any(phrase in command for phrase in ["hulu", "open hulu"]):
        open_website("https://www.hulu.com", "Hulu")
    elif any(phrase in command for phrase in ["twitch", "watch twitch"]):
        open_website("https://www.twitch.tv", "Twitch")
    elif any(phrase in command for phrase in ["amazon", "open amazon", "shop amazon"]):
        open_website("https://www.amazon.com", "Amazon")
    elif any(phrase in command for phrase in ["twitter", "x.com", "go to twitter"]):
        open_website("https://www.twitter.com", "Twitter/X")
    elif any(phrase in command for phrase in ["facebook", "open facebook"]):
        open_website("https://www.facebook.com", "Facebook")
    elif any(phrase in command for phrase in ["instagram", "open instagram"]):
        open_website("https://www.instagram.com", "Instagram")
    elif any(phrase in command for phrase in ["news", "open news", "cnn", "bbc", "nytimes"]):
        open_website("https://www.google.com/news", "News")
    elif any(phrase in command for phrase in ["weather", "check weather"]):
        open_website("https://www.weather.com", "Weather")
    elif any(phrase in command for phrase in ["stackoverflow", "open stackoverflow"]):
        open_website("https://stackoverflow.com", "StackOverflow")

    else:
        logging.warning(f"[CLI] Unrecognized command: {command}")
