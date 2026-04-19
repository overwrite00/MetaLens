"""Auto-register all handlers when this package is imported."""
from core.registry import HandlerRegistry

from core.handlers.filesystem_handler import FilesystemHandler
from core.handlers.image_handler import ImageHandler
from core.handlers.audio_handler import AudioHandler
from core.handlers.video_handler import VideoHandler
from core.handlers.pdf_handler import PdfHandler
from core.handlers.office_handler import OfficeHandler
from core.handlers.ole_handler import OleHandler
from core.handlers.hachoir_handler import HachoirHandler

HandlerRegistry.register(FilesystemHandler())
HandlerRegistry.register(ImageHandler())
HandlerRegistry.register(AudioHandler())
HandlerRegistry.register(VideoHandler())
HandlerRegistry.register(PdfHandler())
HandlerRegistry.register(OfficeHandler())
HandlerRegistry.register(OleHandler())
HandlerRegistry.register(HachoirHandler())
