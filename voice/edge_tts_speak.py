
import asyncio
from typing import Optional
try:
    import edge_tts  # type: ignore
except Exception:
    edge_tts = None

async def _speak_async(text: str, voice: str = "en-US-AriaNeural", outfile: Optional[str] = None):
    if edge_tts is None:
        raise RuntimeError("edge-tts is not installed. pip install edge-tts")
    communicate = edge_tts.Communicate(text, voice=voice)
    if outfile:
        await communicate.save(outfile)
    else:
        import tempfile, os
        from playsound import playsound  # type: ignore
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            await communicate.save(f.name)
            tmp = f.name
        playsound(tmp)
        os.unlink(tmp)

def speak(text: str, voice: str = "en-US-AriaNeural", outfile: Optional[str] = None):
    asyncio.run(_speak_async(text, voice, outfile))
