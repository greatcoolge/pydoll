import logging
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

# Windows 错误码常量
WINERROR_SHARING_VIOLATION = 32

logger = logging.getLogger(__name__)


class TempDirectoryManager:
    """
    Manages temporary directory lifecycle for CDP browser automation.

    Creates isolated temporary directories for browser profiles and handles
    secure cleanup with retry mechanisms for locked files.
    """

    def __init__(self, temp_dir_factory: Callable[[], TemporaryDirectory] = TemporaryDirectory):
        """
        Initialize temporary directory manager.

        Args:
            temp_dir_factory: Function to create temporary directories.
                Must return TemporaryDirectory-compatible object.
        """
        self._temp_dir_factory = temp_dir_factory
        self._temp_dirs: list[TemporaryDirectory] = []
        logger.debug('TempDirectoryManager initialized')

    def create_temp_dir(self) -> TemporaryDirectory:
        """
        Create and track new temporary directory for browser use.

        Returns:
            TemporaryDirectory object for browser --user-data-dir argument.
        """
        temp_dir = self._temp_dir_factory()
        self._temp_dirs.append(temp_dir)
        logger.debug(f'Created temp directory: {temp_dir.name}')
        return temp_dir

    @staticmethod
    def retry_process_file(func: Callable[[str], None], path: str, retry_times: int = 10):
        """
        Execute file operation with retry logic for locked files.

        Args:
            func: Function to execute on path.
            path: File or directory path to operate on.
            retry_times: Maximum retry attempts (negative = unlimited).

        Raises:
            PermissionError: If operation fails after all retries.
        """
        retry_time = 0
        while retry_times < 0 or retry_time < retry_times:
            retry_time += 1
            try:
                func(path)
                break
            except PermissionError:
                time.sleep(0.1)
                logger.debug(
                    f'Retrying file operation due to PermissionError (attempt {retry_time})'
                )
        else:
            raise PermissionError()

    def handle_cleanup_error(self, func: Callable[[str], None], path: str, exc_info: tuple):
        """
        Handle errors during directory cleanup with browser-specific workarounds.

        Args:
            func: Original function that failed.
            path: Path that could not be processed.
            exc_info: Exception information tuple.

        Note:
            Handles Chromium-specific locked files like CrashpadMetrics.
        """
        exc_type, exc_value, _ = exc_info

        if (
            exc_type is PermissionError
            or (
                exc_type is OSError
                and getattr(exc_value, "winerror", None) == WINERROR_SHARING_VIOLATION
            )
        ):
            is_known = self._is_known_locked_file(path)
            logger.debug(f"Path {path} known locked: {is_known}")

        # Windows file lock (PermissionError or OSError winerror=32)
        if (
            exc_type is PermissionError
            or (
                exc_type is OSError
                and getattr(exc_value, "winerror", None) == WINERROR_SHARING_VIOLATION
            )
        ):
            # Fast-path for known locked files
            if self._is_known_locked_file(path):
                try:
                    self.retry_process_file(func, path)
                    return
                except PermissionError:
                    logger.warning(f"Ignoring locked Chrome file during cleanup: {path}")
                    return

            # Generic retry fallback for unknown locked files
            for _ in range(5):
                time.sleep(0.5)
                try:
                    func(path)
                    return
                except PermissionError:
                    continue

            logger.warning(f"Failed to delete after retries: {path}")
            raise exc_value  # 关键：重新抛出原始异常

        raise exc_value

    @staticmethod
    def _is_known_locked_file(path: str) -> bool:
        """Check if path matches known Chromium locked file patterns."""
        path_lc = path.lower()
        known_patterns = [
            'crashpadmetrics-active.pma',
            'safe browsing',
            'safe browsing cookies',
            '\\cache\\',
            '/cache/',
            'no_vary_search',
            'journal.baj',
            '\\network\\cookies',
            '/network/cookies',
            'cookies-journal',
            '\\local storage\\',
            '/local storage/',
            '\\local storage\\leveldb\\',
            '/local storage/leveldb/',
            'leveldb',
            'indexeddb',
            '\\session storage\\',
            '/session storage/',
            'log',
            'reporting and nel',
        ]
        return any(substr in path_lc for substr in known_patterns)

    def cleanup(self):
        """
        Remove all tracked temporary directories with error handling.

        Uses custom error handler for browser-specific file lock issues.
        Continues cleanup even if some files resist deletion.
        """
        for temp_dir in self._temp_dirs:
            logger.info(f'Cleaning up temp directory: {temp_dir.name}')
            shutil.rmtree(temp_dir.name, onerror=self.handle_cleanup_error)
            remaining = Path(temp_dir.name)
            if not remaining.exists():
                continue

            for attempt in range(10):
                time.sleep(0.2)
                try:
                    shutil.rmtree(temp_dir.name, onerror=self.handle_cleanup_error)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
                if not remaining.exists():
                    logger.debug(
                        f'Temp directory removed after retry #{attempt + 1}: {temp_dir.name}'
                    )
                    break
            if remaining.exists():
                logger.warning(
                    f'Temp directory still present after retries (leftover files may remain): '
                    f'{temp_dir.name}'
                )
