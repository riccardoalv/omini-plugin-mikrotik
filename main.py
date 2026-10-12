"""Omini plugin for MikroTik RouterOS (entrypoint run by the Omini core)."""

from omini_sdk import plugin

from omini_mikrotik.collect import apply, backup, collect, test

plugin.collect(collect)
plugin.test(test)
plugin.apply(apply)
plugin.backup(backup)

if __name__ == "__main__":
    plugin.run()
