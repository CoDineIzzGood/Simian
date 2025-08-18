
import json
import os
from datetime import datetime
import sys
import traceback

def log_crash(exc_type, exc_value, exc_traceback):
    with open("logs/crash.log", "a", encoding="utf-8") as f:
        f.write("=== CRASH DETECTED ===\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)

sys.excepthook = log_crash

class MemoryManager:
    def __init__(self, db_path="memory/simian_memory.db"):
        self.db_path = db_path
        # ✅ Ensure directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if not os.path.exists(self.db_path):
            self._initialize_memory()
        self.memory = self._load_memory()

    def _initialize_memory(self):
        default_memory = {
            "conversations": [],
            "short_term": {},
            "long_term": {}
        }
        with open(self.db_path, "w") as f:
            json.dump(default_memory, f, indent=4)

    def _load_memory(self):
        with open(self.db_path, "r") as f:
            return json.load(f)

    def save_memory(self):
        with open(self.db_path, "w") as f:
            json.dump(self.memory, f, indent=4)

    def log_voice_command(self, voice_command: str):
        if "voice_commands" not in self.memory:
            self.memory["voice_commands"] = []
        self.memory["voice_commands"].append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "command": voice_command
        })
        self.save_memory()


    def store_conversation(self, user_input, simian_response):
        self.memory["conversations"].append({
            "user": user_input,
            "simian": simian_response
        })
        self.save_memory()

    def get_recent_conversation(self, limit=5):
        return self.memory["conversations"][-limit:]

    def clear_memory(self):
        self._initialize_memory()
        self.memory = self._load_memory()

