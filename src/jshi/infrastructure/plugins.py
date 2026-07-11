from __future__ import annotations

from collections.abc import Iterable

from jshi.core import Plugin


class PluginRegistry:
    def __init__(self, plugins: Iterable[Plugin] = ()) -> None:
        self._plugins: dict[str, Plugin] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        if name in self._plugins:
            raise ValueError(f"Plugin already registered: {name}")
        if not plugin.healthcheck():
            raise ValueError(f"Plugin is unhealthy: {name}")
        plugin.initialize()
        self._plugins[name] = plugin

    def replace(self, plugin: Plugin) -> Plugin | None:
        if not plugin.healthcheck():
            raise ValueError(f"Plugin is unhealthy: {plugin.manifest.name}")
        previous = self._plugins.get(plugin.manifest.name)
        if previous:
            plugin.migrate(previous.manifest.version, previous.export_state())
            previous.shutdown()
        plugin.initialize()
        self._plugins[plugin.manifest.name] = plugin
        return previous

    def all(self) -> tuple[Plugin, ...]:
        return tuple(self._plugins.values())

    def get(self, name: str) -> Plugin:
        return self._plugins[name]

    def shutdown(self) -> None:
        for plugin in self._plugins.values():
            plugin.shutdown()
