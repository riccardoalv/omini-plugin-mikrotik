"""Omini plugin for MikroTik RouterOS (entrypoint run by the Omini core)."""

from omini_sdk import plugin

from omini_mikrotik.collect import apply, collect, test

plugin.collect(collect)
plugin.test(test)
plugin.apply(apply)

if __name__ == "__main__":
    plugin.run()
