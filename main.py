from desktop_notifier import DesktopNotifier
from flask import Flask, request
from unidecode import unidecode
from locked import Locked
from piper_backend import Piper
from speechd_backend import Speechd
import argparse
import logging
import shutil
import time
import datetime
import subprocess

logger = logging.getLogger(__name__)


class App:
    def __init__(self, parsed):
        self.parsed = parsed

        # Apply defaults if not set
        if self.parsed.speechd:
            self.parsed.speed = 0 if self.parsed.speed is None else self.parsed.speed
            self.parsed.volume = 100 if self.parsed.volume is None else self.parsed.volume
        else:
            self.parsed.speed = 1 if self.parsed.speed is None else self.parsed.speed
            self.parsed.volume = 1 if self.parsed.volume is None else self.parsed.volume
        self.contain_speed_volume()

        self.begin_time = time.time()
        self.notifier = DesktopNotifier()

        self.flask = Flask("tts-reader")
        self.flask.add_url_rule(
            "/read", "read", view_func=self.read, methods=["GET", "POST"]
        )
        self.flask.add_url_rule("/play", "play", view_func=self.play)
        self.flask.add_url_rule("/pause", "pause", view_func=self.pause)
        self.flask.add_url_rule("/toggle", "toggle", view_func=self.toggle)
        self.flask.add_url_rule("/reset", "reset", view_func=self.reset)
        self.flask.add_url_rule("/skip", "skip", view_func=self.skip)
        self.flask.add_url_rule("/volume/<float:data>", "volume", view_func=self.volume)
        self.flask.add_url_rule("/speed/<float:data>", "speed", view_func=self.speed)
        self.flask.add_url_rule("/status", "status", view_func=self.status)

        self.wlpaste_path = shutil.which("wl-paste")
        self.xclip_path = shutil.which("xclip")
        if self.parsed.wayland is True:
            if self.wlpaste_path is None:
                raise Exception("Couldn't find the wl-paste binary")
        else:
            if self.xclip_path is None:
                raise Exception("Couldn't find the xclip binary")

        self.tts = Speechd(self.parsed) if self.parsed.speechd else Piper(self.parsed)
        if not self.tts.inited:
            raise Exception("Failed to initialize the TTS backend")

    def contain_speed_volume(self):
        if self.parsed.speechd:
            self.parsed.volume = max(-100, min(self.parsed.volume, 100))
            self.parsed.speed = max(-100, min(self.parsed.speed, 100))
        else:
            self.parsed.volume = max(0.0, min(self.parsed.volume, 2.0))
            self.parsed.speed = max(0.0, min(self.parsed.speed, 5.0))

    def read(self):
        num_chars = 0

        getaudio = request.args.get("getaudio", None) is not None

        if request.method == "POST":
            if len(request.data) > 0:
                try:
                    text = request.data.decode("utf-8")
                except UnicodeError as e:
                    s = "Failed to decode the POSTed data as UTF-8"
                    logger.error("%s: %s", s, repr(e))
                    self.notify(s)
                    return s

                num_chars = len(text)

            else:
                s = "Failed to get the POSTed data"
                logger.error(
                    "%s: Empty post request maybe because the content type header (%s) is wrong",
                    s,
                    request.content_type,
                )
                self.notify(s)
                return s

        else:
            try:
                out = subprocess.run(
                    [self.wlpaste_path, "-p"]
                    if self.parsed.wayland
                    else [self.xclip_path, "-o", "-selection primary"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout

                try:
                    text = out.decode("utf-8")
                except UnicodeError as e:
                    s = "Failed to decode the selection clipboard data as UTF-8"
                    logger.error("%s: %s", s, repr(e))
                    self.notify(s)
                    return s

                num_chars = len(text)

            except subprocess.CalledProcessError as e:
                s = "Failed to get the clipboard contents. Maybe the selection clipboard is empty?"
                logger.error("%s: %s", s, repr(e))
                self.notify(s)
                return s

        text = unidecode(text.strip()).replace("‐\n", "").replace("‐ ", "")
        if len(text) == 0:
            s = "Skipped processing empty text"
            self.notify(s)
            return s

        s = f"Queued text of {num_chars} characters for the TTS"
        self.notify(s)

        audio = self.tts.speak(text, getaudio)

        return audio if getaudio else s

    def status(self):
        return {
            "self": {
                "uptime()": self.uptime(),
                "parsed": self.parsed.__dict__,
            },
            "self.tts": self.tts.status(),
        }

    def toggle(self):
        self.tts.toggle()
        return ""

    def play(self):
        self.tts.play()
        return ""

    def pause(self):
        self.tts.pause()
        return ""

    def reset(self):
        self.tts.reset()
        return ""

    def skip(self):
        self.tts.skip()
        return ""

    def speed(self, data):
        self.parsed.speed = data
        self.contain_speed_volume()
        return ""

    def volume(self, data):
        self.parsed.volume = data
        self.contain_speed_volume()
        return ""

    def uptime(self):
        diff = time.time() - self.begin_time
        return str(datetime.timedelta(seconds=int(diff)))

    def run(self):
        self.flask.run(
            host=self.parsed.ip, port=self.parsed.port, debug=self.parsed.debug
        )

    def notify(self, msg):
        self.notifier.send_sync(title="TTS Reader", message=msg, timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="tts-reader",
    )
    parser.add_argument("--ip", type=str, default="127.0.0.1", help="IP address")
    parser.add_argument("--port", type=int, default=5000, help="Port")
    parser.add_argument(
        "--wayland",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Assume running under Wayland",
    )
    parser.add_argument(
        "--piper-python",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Attempt to use the piper python module. Has no effect if a different backend is selected",
    )
    parser.add_argument(
        "--speechd",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Use speechd instead of piper. Incomplete",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=None,
        help="Volume. Piper: [0-2, def:1], Speechd: [-100-100, def:100]",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help="Speech rate. Piper: [0-5, def:1], Speechd: [-100-100, def:0]",
    )
    parser.add_argument(
        "--piper-rate",
        type=int,
        default=22050,
        help="Piper: Playback sample rate. More info at https://github.com/rhasspy/piper/blob/master/TRAINING.md",
    )
    parser.add_argument(
        "--piper-sentence-silence",
        type=float,
        default=0.8,
        help="Piper: Seconds of silence after each sentence",
    )
    parser.add_argument(
        "--piper-one-sentence",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Piper: Process one sentence at a time, instead of the default whole selection",
    )
    parser.add_argument(
        "--piper-model", type=str, default=None, help="Piper: Path to the model"
    )
    parser.add_argument(
        "--piper-model-config",
        type=str,
        default=None,
        help="Piper: Path to the model configuration",
    )
    parser.add_argument(
        "--debug",
        default=False,
        action=argparse.BooleanOptionalAction,
        help="Enable flask debug mode (developmental purposes)",
    )

    parsed = parser.parse_args()

    logging.basicConfig(
        encoding="utf-8", level=logging.DEBUG if parsed.debug else logging.INFO
    )

    app = App(parsed)
    app.run()
