import logging
import os

# Must be set before web.app is imported so control endpoints know they're in
# dashboard-only mode and can't reach the engine process.
os.environ["KAIROS_DASHBOARD_ONLY"] = "1"

from bot import __version__
from bot.db import init_db

LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "dashboard.log")


def setup_logging():
    import zoneinfo

    _tz = zoneinfo.ZoneInfo("Europe/Helsinki")

    def _localtime(t):
        import datetime
        return datetime.datetime.fromtimestamp(t, tz=_tz).timetuple()

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fmt = logging.Formatter(f"%(asctime)s  v{__version__}  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fmt.converter = _localtime

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
    from web.app import run
    run()
