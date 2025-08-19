
def transcribe_from_mic():
    try:
        import speech_recognition as sr  # type: ignore
    except Exception:
        return None
    r = sr.Recognizer()
    with sr.Microphone() as source:
        audio = r.listen(source, timeout=3, phrase_time_limit=10)
    try:
        return r.recognize_google(audio)
    except Exception:
        return None
