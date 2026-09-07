from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_evidence import file_sha256
from story_module_adapters import MockImageGeneratorAdapter
from story_module_ports import (
    ImageGeneratorPort,
    ImageGeneratorRequest,
    ImageGeneratorResult,
    ModuleCapabilities,
    ModuleIdentity,
)
from story_module_registry import (
    MODULE_EXECUTION_MODE_ENV,
    MODULE_EXECUTION_MODE_REQUIRED_ENV,
    MODULE_PROFILE_ENV,
    MODULE_PROFILE_REQUIRED_ENV,
    ModuleRegistry,
    build_registry_for_profile,
)
from story_project import init_project, project_paths, write_manifest
from tests.test_visual_sample_gate import _fixture
from visual_sample_gate import compile_visual_sample_plan, visual_sample_paths


def _mock_image_environment() -> dict[str, str]:
    return {
        MODULE_PROFILE_ENV: "mock-image",
        MODULE_PROFILE_REQUIRED_ENV: "mock-image",
        MODULE_EXECUTION_MODE_ENV: "test",
        MODULE_EXECUTION_MODE_REQUIRED_ENV: "test",
    }









if __name__ == "__main__":
    unittest.main()
