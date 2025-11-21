"""
Module for loading, accessing, and saving JSON-configuration files.

Provides a simple `ConfigLoader` class that reads a JSON file into a dict, allows
property retrieval, and writes updates back to disk.
"""

import json
from typing import Any, Optional


class ConfigLoader:
    """
    Manages a JSON-based configuration file.

    Attributes:
        file_path (str): Path to the JSON config file.
        config (dict): Internal dictionary holding the configuration data.
    """

    def __init__(self, file_path: str):
        """
        Initialize the ConfigLoader.

        Automatically loads the config from the given file path.

        Args:
            file_path (str): The path to the JSON configuration file.
        """
        self.file_path = file_path
        self.config: dict[str, Any] = {}
        self.load_config()

    def load_config(self) -> None:
        """
        Load the configuration from the JSON file into memory.

        Reads the file at `self.file_path` and parses it into the `config` dict.

        Raises:
            FileNotFoundError: If the file does not exist.
            json.JSONDecodeError: If the file contains invalid JSON.
        """
        with open(self.file_path, 'r') as file:
            self.config = json.load(file)

    def get_property(self, key: str) -> Optional[Any]:
        """
        Get a configuration value by key.

        Args:
            key (str): The configuration key to retrieve.

        Returns:
            The value for that key, or None if the key is not present.
        """
        return self.config.get(key)

    def save_config(self) -> None:
        """
        Save the current configuration back to disk.

        Writes the current contents of `self.config` as JSON to `self.file_path`
        with indentation for readability.

        Raises:
            IOError: If the file cannot be opened for writing.
        """
        with open(self.file_path, 'w') as file:
            json.dump(self.config, file, indent=4)
