"""Cloud entrypoint enabling Phase 12 Telegram operations and two-user Drive routing."""
import main as app
from config import Config
from long_media_runtime import install_long_media_runtime
from quality_rescue_runtime import install_quality_rescue_runtime
from retry_epoch_runtime import install_retry_epoch_runtime
from telegram_commands import install_phase12_runtime


def install_drive_routing_runtime(app_module) -> None:
    """Reset output routing after Drive ingestion and before Telegram/YouTube work.

    The cloud one-shot cycle always lists/processes Drive first and then calls
    ``get_telegram_updates``. Drive downloads activate their owner-specific
    output folder. Wrapping the Telegram boundary restores Primary's legacy output
    before any Telegram or YouTube pipeline begins, preventing a preceding Secondary
    job from leaking its output route into another source.
    """
    if getattr(app_module, "_phase12_5_drive_routing_installed", False):
        return

    original_get_updates = app_module.get_telegram_updates

    def get_updates_with_default_drive_route(*args, **kwargs):
        Config.activate_default_drive_route()
        return original_get_updates(*args, **kwargs)

    app_module.get_telegram_updates = get_updates_with_default_drive_route
    app_module._phase12_5_drive_routing_installed = True


def run() -> None:
    install_phase12_runtime(app)
    install_drive_routing_runtime(app)
    install_quality_rescue_runtime()
    # Install retry-epoch reset before long-media wraps Firestore save_chunk so
    # durable progress is acknowledged before a cooperative batch yield can fire.
    install_retry_epoch_runtime()
    install_long_media_runtime(app)
    app.main()


if __name__ == "__main__":
    run()
