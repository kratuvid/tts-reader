from subprocess import Popen
from queue import Queue
import argparse
import datetime
import os
import signal
import subprocess
import sys
import shutil
import threading
import time
import logging
import config

logging.basicConfig(encoding="utf-8", level=logging.INFO)
logger = logging.getLogger(__name__)

has_speechd = True
try:
    import speechd
except ImportError:
    logger.warning("speechd is unavailable")
    has_speechd = False

from flask import Flask, request
from desktop_notifier import DesktopNotifier
from unidecode import unidecode

parser = argparse.ArgumentParser(
    prog="tts-reader",
)
parser.add_argument("-i", "--ip", type=str, default="127.0.0.1", help="IP address")
parser.add_argument("-p", "--port", type=int, default=5000, help="Port")
parser.add_argument(
    "-s", "--playback_speed", type=float, default=1.0, help="Playback speed"
)
parser.add_argument("-v", "--volume", type=float, default=1.0, help="Volume [0-1]")
parser.add_argument(
    "-r",
    "--playback_sample_rate",
    type=int,
    default=22050,
    help="Playback sample rate. More info at https://github.com/rhasspy/piper/blob/master/TRAINING.md",
)
parser.add_argument(
    "-l",
    "--sentence_silence",
    type=float,
    default=0.7,
    help="Seconds of silence after each sentence. Passed to piper",
)
parser.add_argument(
    "-o",
    "--one_sentence",
    default=False,
    action=argparse.BooleanOptionalAction,
    help="Process one sentence at a time, instead of the default whole selection",
)
parser.add_argument(
    "-w",
    "--wayland",
    default=False,
    action=argparse.BooleanOptionalAction,
    help="Assume running under Wayland",
)
parser.add_argument(
    "-e",
    "--speechd",
    default=False,
    action=argparse.BooleanOptionalAction,
    help="Use speechd instead of piper",
)
parser.add_argument("-m", "--model", type=str, default=None, help="Path to the model")
parser.add_argument(
    "-c",
    "--model_config",
    type=str,
    default=None,
    help="Path to the model configuration",
)
parser.add_argument(
    "-d",
    "--debug",
    default=False,
    action=argparse.BooleanOptionalAction,
    help="Enable flask debug mode (developmental purposes)",
)

parsed = None
pass_queue = Queue()
pass_queue_size = 0
pass_queue_size_lock = threading.Lock()
gen_process = None
play_process = None
stop_event = threading.Event()
begin_time = None
paused = False
notifier = DesktopNotifier()
ffplay_path = None
piper_path = None
sdclient = speechd.SSIPClient("tts-reader") if has_speechd else None

app = Flask("tts-reader")


def thread_play():
    global play_process
    global stop_event
    global pass_queue
    global pass_queue_size
    global pass_queue_size_lock

    while True:
        audio = pass_queue.get()
        try:
            if not stop_event.is_set():
                play_process = Popen(
                    [
                        ffplay_path,
                        "-hide_banner",
                        "-loglevel",
                        "panic",
                        "-nostats",
                        "-autoexit",
                        "-nodisp",
                        "-af",
                        f"atempo={parsed.playback_speed},volume={parsed.volume}",
                        "-f",
                        "s16le",
                        "-ar",
                        f"{parsed.playback_sample_rate}",
                        "-ac",
                        "1",
                        "-",
                    ],
                    stdin=subprocess.PIPE,
                    start_new_session=True,
                )
                play_process.communicate(input=audio)

        except Exception as e:
            s = "Failed to play"
            logger.critical("%s: %s", s, str(e))
            notify(s)
            fatal_exit()

        finally:
            play_process = None
            pass_queue.task_done()
            with pass_queue_size_lock:
                pass_queue_size -= len(audio)
                pass_queue_size = 0 if pass_queue_size < 0 else pass_queue_size


play_thread = threading.Thread(target=thread_play, daemon=True)
play_thread.start()


@app.route("/read", methods=["POST", "GET"])
def read():
    global pass_queue_size
    global pass_queue_size_lock
    global stop_event
    stop_event.clear()

    num_chars = 0

    try:
        if request.method == "POST":
            if len(request.data) > 0:
                text = request.data.decode("utf-8")
                num_chars = len(text)
            else:
                s = "Failed to get the POSTed data"
                logger.error(
                    "%s: Empty post request maybe because the content type header (%s) is wrong",
                    s,
                    request.content_type,
                )
                notify(s)
                return s

        else:
            out = subprocess.check_output(
                ["wl-paste", "-p"]
                if parsed.wayland
                else ["xclip", "-o", "-selection primary"]
            )
            text = out.decode("utf-8")
            num_chars = len(text)

    except Exception as e:
        s = "Failed to get the text"
        logger.error("%s: %s", s, str(e))
        notify(s)
        return s

    notify(f"Requested {num_chars} characters to be read")

    sendaudio = request.args.get("sendaudio", None)

    text = sanitize_text(text)

    try:
        if parsed.one_sentence:
            tokens = text.split(". ")
            while tokens and not stop_event.is_set():
                text = tokens[0].strip() + "."
                tokens = tokens[1:]

                out = generate_audio(text)
                if out is not None:
                    if sendaudio is not None:
                        return out
                    else:
                        pass_queue.put(out)
                        with pass_queue_size_lock:
                            pass_queue_size += len(out)
        else:
            out = generate_audio(text)
            if out is not None:
                if sendaudio is not None:
                    return out
                else:
                    pass_queue.put(out)
                    with pass_queue_size_lock:
                        pass_queue_size += len(out)

    except Exception as e:
        s = "Failed while organizing/generating text"
        logger.critical("%s: %s", s, str(e))
        notify(s)
        fatal_exit()

    play()

    return f"Generated and queued {num_chars} characters for playback"


def generate_audio(text):
    global gen_process

    try:
        gen_process = Popen(
            [
                piper_path,
                "--output_raw",
                "--sentence_silence",
                f"{parsed.sentence_silence}",
                "--model",
                parsed.model,
                "--config",
                parsed.model_config,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        out, _ = gen_process.communicate(input=text.encode())

    except Exception as e:
        s = "Failed while generating audio"
        logger.critical("%s: %s", s, str(e))
        notify(s)
        fatal_exit()

    finally:
        gen_process = None

    return out


def sanitize_text(text: str):
    return unidecode(text).replace("‐\n", "").replace("‐ ", "")


@app.route("/stop")
def stop():
    global stop_event
    global pass_queue
    global pass_queue_size
    global pass_queue_size_lock
    global paused

    num_queue = pass_queue.qsize()

    stop_event.set()
    paused = False

    while pass_queue.qsize() > 0:
        pass_queue.get()
    with pass_queue_size_lock:
        pass_queue_size = 0

    try:
        if gen_process is not None:
            logger.debug(f"Killing gen_process {gen_process.pid}")
            os.killpg(gen_process.pid, signal.SIGTERM)

        if play_process is not None:
            logger.debug(f"Killing play_process {play_process.pid}")
            os.killpg(play_process.pid, signal.SIGTERM)

    except Exception as e:
        s = "Failed to stop TTS"
        logger.error("%s: %s", s, str(e))
        notify(s)

    return f"Queue cleared of pending {num_queue} items. Killed the generate and play processes if running"


@app.route("/status")
def status():
    return (
        f"Generator process running? {'Yes at ' + str(gen_process.pid) if gen_process is not None else 'No'}\n"
        + f"Playback process running? {'Yes at ' + str(play_process.pid) if play_process is not None else 'No'}\n"
        + f"Playback speed? {parsed.playback_speed}\n"
        + f"Playback volume? {parsed.volume}\n"
        + f"Playback paused? {paused}\n"
        + f"Pending queue length? {pass_queue.qsize()}\n"
        + f"Queue size? {pass_queue_size} B, {pass_queue_size/1024:.2f} KB, {pass_queue_size/(1024**2):.2f} MB\n"
        + f"Stop signal issued? {stop_event.is_set()}\n"
        + f"Uptime? {uptime()}"
    )


@app.route("/speed/<float:playback_speed>")
def speed(playback_speed):
    global parsed
    parsed.playback_speed = playback_speed
    s = f"Playback speed is now {parsed.playback_speed}"
    logger.info(s)
    notify(s)
    return s


@app.route("/volume/<float:playback_volume>")
def volume(playback_volume):
    global parsed
    parsed.volume = playback_volume
    s = f"Playback volume is now {parsed.volume}"
    logger.info(s)
    notify(s)
    return s


@app.route("/play")
def play():
    global paused
    paused = False
    if play_process is not None:
        os.kill(play_process.pid, signal.SIGCONT)
        return "Playback continued"
    return "Nothing playing"


@app.route("/pause")
def pause():
    global paused
    if play_process is not None:
        os.kill(play_process.pid, signal.SIGSTOP)
        paused = True
        return "Playback paused"
    return "Nothing playing"


@app.route("/toggle")
def toggle():
    if paused:
        return play()
    else:
        return pause()


@app.route("/skip")
def skip():
    global paused
    paused = False
    if play_process is not None:
        os.kill(play_process.pid, signal.SIGCONT)
        os.killpg(play_process.pid, signal.SIGTERM)
        return "Skipped current playing"
    return "Nothing playing"


def notify(msg):
    global notifier
    notifier.send_sync(title="TTS Reader", message=msg, timeout=2)


def uptime():
    diff = time.time() - begin_time
    return str(datetime.timedelta(seconds=int(diff)))


def fatal_exit():
    os.kill(os.getpid(), signal.SIGTERM)


if __name__ == "__main__":
    begin_time = time.time()
    parsed = parser.parse_args()

    logger.setLevel(logging.DEBUG if parsed.debug else logger.level)

    ffplay_path = shutil.which("ffplay")
    if ffplay_path is None:
        logger.critical("ffplay is not in your system PATH")
        fatal_exit()
    else:
        logger.info("Found the ffplay binary")

    if parsed.speechd:
        if not has_speechd:
            logger.critical(
                "The speechd python module is unavailable. Please check if you have speech-dispatcher installed and/or you've enabled --system-site-packages for the virtualenv"
            )
            fatal_exit()
        else:
            logger.info("Found the speechd module")
    else:
        piper_path = shutil.which("piper-tts")
        if piper_path is None:
            logger.critical("piper-tts is not in your system PATH")
            fatal_exit()
        else:
            logger.info("Found the piper-tts binary")

    if parsed.model is None or parsed.model_config is None:
        logger.critical("Please provide the --model and --model_config")
        fatal_exit()
    else:
        logger.debug(
            "Provided model is %s with configuration %s",
            parsed.model,
            parsed.model_config,
        )

    app.run(host=parsed.ip, port=parsed.port, debug=parsed.debug)
