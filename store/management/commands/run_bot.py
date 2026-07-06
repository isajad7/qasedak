from store.management.commands.run_telegram_polling import Command as TelegramPollingCommand


class Command(TelegramPollingCommand):
    help = "Run the Qasedak tenant Telegram bot worker."
