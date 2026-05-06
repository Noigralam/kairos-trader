from bot.db import init_db
from web.app import run

if __name__ == "__main__":
    init_db()
    run()
