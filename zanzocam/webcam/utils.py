import sys
import logging
import datetime
import traceback
import constants
from webcam.errors import UnexpectedServerResponse


# Setup the logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[
        logging.FileHandler(constants.CAMERA_LOG),
        logging.StreamHandler(sys.stdout),
    ]
)


def log(msg: str) -> None:
    """ 
    Logs the message to the console
    """
    logging.info(f"{datetime.datetime.now()} -> {msg}")


def log_error(msg: str, e: Exception=None, fatal: str=None, print_server_errors: bool = False) -> None:
    """
    Logs an error to the console
    """
    fatal_msg = ""
    if fatal is not None:
        fatal_msg = f"THIS ERROR IS FATAL: {fatal}"

    stacktrace = ""
    if e is not None and (not isinstance(e, UnexpectedServerResponse) or print_server_errors):
        stacktrace = f"The exception is: {str(e)}\n\n{traceback.format_exc()}\n"

    log(f"ERROR! {msg} {fatal_msg} {stacktrace}")
    
    

def log_row(char: str = "=") -> None:
    """ 
    Logs a row to the console
    """
    logging.info(f"\n{char*50}\n")