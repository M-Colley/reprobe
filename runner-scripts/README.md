# runner-scripts/

The Python/R/Jupyter runners build their container commands inline (see
`src/reprobe/runners/*.py`), so no scripts need to be copied into runtime images
for the MVP.

This directory holds small scripts that the **Unity T1/T2 tiers** (Phase 3) inject
into the GameCI editor container — they cannot be expressed as a one-line shell
command. `unity_compile_check.cs` is the T1 (compile) entry point, kept here as a
reference for when a Unity seat is available.
