"""Qlib file storage with instance configuration for registered Lab inputs."""

from qlib.config import QlibConfig
from qlib.data.storage import CalendarStorage
from qlib.data.storage.file_storage import FileCalendarStorage, FileFeatureStorage


class _PinnedDataPath:
    @property
    def dpm(self):
        # Registered publications are local paths; they never use global NFS
        # mount configuration, which may belong to another market or be unset.
        return QlibConfig.DataPathManager(self.provider_uri, {})


class PinnedCalendarStorage(_PinnedDataPath, FileCalendarStorage):
    def __init__(self, provider_uri, region):
        # The official calendar constructor reads global C['region']. Bind the
        # same storage fields locally so cold API processes need no qlib.init.
        CalendarStorage.__init__(self, "day", False)
        self._provider_uri = QlibConfig.DataPathManager.format_provider_uri(
            provider_uri
        )
        self.region = region
        self.enable_read_cache = False


class PinnedFeatureStorage(_PinnedDataPath, FileFeatureStorage):
    pass
