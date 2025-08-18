
import speech_recognition as sr

def recognize_command():
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening...")
        audio = recognizer.listen(source)

    try:
        command = recognizer.recognize_google(audio)
        print("Recognized:", command)
        return command
    except sr.UnknownValueError:
        return "Sorry, I didn't catch that."
    except sr.RequestError as e:
        return f"Request failed: {e}"
