import logging
import os
from bot.db import init_db
from web.app import run

LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "bot.log")


def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s UTC  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fmt.converter = __import__("time").gmtime  # UTC timestamps

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger = logging.getLogger("cryptobot")
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


if __name__ == "__main__":
    setup_logging()
    init_db()
    from bot.simulator import init as init_state
    from bot.engine import start as start_engine
    init_state()
    start_engine()
    run()
