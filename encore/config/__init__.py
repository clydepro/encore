"""Installation-time configuration: loading, validation and read-only summary.

Implements the model described in SAPRS Chapter 12: a YAML file supplied at
installation time, validated at startup, and never used as managed state.

* Invalid configuration must prevent startup with a clear diagnostic.
* Secrets come from the environment or system store, never from source.
"""
