import logging
import sys


def setup_logging() -> None:
    logger = logging.getLogger()
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname).4s - %(message)s - (%(filename)s:%(lineno)s)'))
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)
