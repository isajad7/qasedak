from .base import *


DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    ["127.0.0.1", "localhost", "panel.vawmusic.ir", "panel.wavmusic.ir", "194.5.195.122"],
)

SMSFORWARDER_WEBHOOK_TOKEN = os.environ.get(
    "SMSFORWARDER_WEBHOOK_TOKEN",
    "81617987833f92953bfeb3b58e7fdaec6fb26c4368db5a0ab70d6017a5b44f70",
)
