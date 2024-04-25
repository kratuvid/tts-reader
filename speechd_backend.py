from tts import TTS
import logging
import time

logger = logging.getLogger(__name__)

has_speechd = True
try:
    import speechd
except ImportError:
    has_speechd = False


class Speechd(TTS):
    def __init__(self, parsed):
        super().__init__()

        if not has_speechd:
            logger.critical(
                "The speechd python module is unavailable. Please check if you have speech-dispatcher installed and/or you've enabled --system-site-packages for the virtualenv"
            )
            self.inited = False
            return

        self.parsed = parsed

        self.sdclient = speechd.SSIPClient(f"tts-reader_{__name__}_{time.time()}")
        self.sdclient.set_priority(speechd.Priority.TEXT)

        self.paused = False

        self.inited = True

    def speak(self, text, getaudio):
        if getaudio:
            e = "The speech dispatcher backend doesn't support downloading audio!"
            logger.error(e)
            return e

        self.play()

        self.sdclient.set_rate(int(self.parsed.speed))
        self.sdclient.set_volume(int(self.parsed.volume))
        self.sdclient.speak(
            text,
            self.speechd_callback,
            (
                speechd.CallbackType.BEGIN,
                speechd.CallbackType.END,
                speechd.CallbackType.CANCEL,
                speechd.CallbackType.PAUSE,
                speechd.CallbackType.RESUME,
            ),
        )

    def speechd_callback(self, type):
        if type in (speechd.CallbackType.END, speechd.CallbackType.CANCEL):
            self.paused = False

    def play(self):
        self.paused = False
        self.sdclient.resume()

    def pause(self):
        self.paused = True
        self.sdclient.pause()

    def toggle(self):
        if self.paused:
            self.play()
        else:
            self.pause()

    def skip(self):
        logger.error("The speech dispatcher backend doesn't support skipping. Ignoring")

    def reset(self):
        self.sdclient.cancel()

    def status(self):
        return {
            "paused": self.paused,
        }
