"""Object-storage provider adapters."""

from aura_core.providers.object_storage.garage import GarageS3ObjectStore
from aura_core.providers.object_storage.image_inspection import PillowImageInspector

__all__ = ["GarageS3ObjectStore", "PillowImageInspector"]
